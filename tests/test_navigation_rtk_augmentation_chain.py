"""Round C：Navigation RTK augmentation 可规划 / 可补盲 / 可布站完整链测试。

覆盖 RequiredCNS → Navigation augmentation evidence → P14 → P15 → P16 →
hypothetical rerun 的完整闭环，以及 fail-closed、依赖、invariant 与 invalidation。
"""

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.corridor.v1 import CNSServiceCorridorV1
from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from cns_planner.application.corridor_site_planning_service import _targets
from cns_planner.application.navigation_reference_station_planning_service import (
    ACTION_TYPE_ADD_REFERENCE_STATION,
    hypothetical_navigation_sites,
    navigation_evidence_required,
    navigation_reference_station_candidate_actions,
)
from cns_planner.application.site_candidate_actions import candidate_actions
from cns_planner.domain.cns_corridor import normalize_cns_corridor_policy
from cns_planner.domain.cns_inputs import normalize_aircraft_profile, normalize_required_cns
from cns_planner.domain.cns_service_contract import (
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_NAVIGATION,
    SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
    distinct_site_id_for,
)
from cns_planner.domain.cns_service_registry import planner_family_for
from cns_planner.domain.navigation_augmentation import (
    CAUSE_CORRECTION_DELIVERY_DEFICIT,
    CAUSE_REFERENCE_STATION_DEFICIT,
    EQUIPMENT_SELECTION_STATUS,
    PLANNING_UNIT_MATURITY,
    REFERENCE_STATION_PLANNING_UNIT,
    REASON_BASELINE_MISSING,
    REASON_DELIVERY_NOT_CONFIGURED,
    REASON_POLICY_NOT_CONFIRMED,
    REASON_SITE_COUNT_MISSING,
    build_navigation_service_evidence,
    collect_navigation_sites,
    evidence_for_probe,
    normalize_navigation_site_suitability,
)
from cns_planner.services.invalidation import DEPENDENTS


RTK = SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION
COMMUNICATION = SERVICE_KEY_COMMUNICATION
PLANNER_FAMILY = "navigation_reference_station"

#: 明确标注：这些只是**测试输入**。生产代码中不存在任何默认基线距离。
TEST_BASELINE_M = 15000


# ---------------------------------------------------------------------------
# fixtures / builders
# ---------------------------------------------------------------------------


def suitability(*, confirmed=True, planning_use=True, **extra):
    value = {"confirmed": confirmed, "planning_use_confirmed": planning_use}
    value.update(extra)
    return value


def tower_colocation_site(site_id, longitude, latitude, *, suitability_value=None, tower_id=None):
    site = {
        "site_id": site_id, "name": site_id,
        "coordinate": [longitude, latitude],
        "available_subsystems": ["N"],
        "planning_profile": {"confirmed": True, "reuse_class": "tower_colocation_host",
                             "add_device_allowed": True},
        "vertical_profile": {"confirmed": True, "service_origin_egm2008_m": 20.0},
        "metadata": {"host": {"host_type": "tower", "host_tower_id": tower_id or site_id}},
    }
    if suitability_value is not None:
        value = dict(suitability_value)
        # 这些测试用它充当**基线已建成**的参考站：显式声明"已建成"。
        value.setdefault("reference_station_installed", True)
        site["navigation_site_suitability"] = value
    return site


def existing_facility(facility_id, site_id, longitude, latitude, *, suitability_value=None):
    site = {
        "facility_id": facility_id, "site_id": site_id, "name": facility_id,
        "coordinate": [longitude, latitude], "coordinate_system": "wgs84",
        "status": "active", "devices": [],
        "planning_profile": {"confirmed": True, "reuse_class": "existing_cns_facility",
                             "add_device_allowed": True},
        "vertical_profile": {"confirmed": True, "service_origin_egm2008_m": 10.0},
    }
    if suitability_value is not None:
        site["navigation_site_suitability"] = suitability_value
    return site


def candidate_site(site_id, longitude, latitude, *, suitability_value=None):
    site = {
        "site_id": site_id, "name": site_id, "coordinate": [longitude, latitude],
        "usable": True, "locked": False,
        "planning_profile": {"confirmed": True, "reuse_class": "candidate_site",
                             "add_device_allowed": True},
        "vertical_profile": {"confirmed": True, "service_origin_egm2008_m": 15.0},
    }
    if suitability_value is not None:
        site["navigation_site_suitability"] = suitability_value
    return site


def communication_requirement():
    """测试用 C 子系统要求（已确认且完整），用于生成 canonical 通信服务证据。"""

    return {
        "required": True, "confirmed": True,
        "coverage_requirement": 10000.0,
        "max_gap_m": 5000.0, "latency_ms": 1000.0, "redundancy": 2,
        "source": "test",
        "services": {COMMUNICATION: {
            "required": True, "service_key": COMMUNICATION, "confirmed": True,
            "performance": {"min_redundancy": 2},
        }},
    }


def required(
    *, planning=None, route_planning=None, communication_required=False, navigation_extra=None,
):
    navigation = {"required": True}
    if planning is not None or route_planning is not None:
        navigation["services"] = {RTK: {
            "required": True, "service_key": RTK,
            "planning": planning if planning is not None else {},
        }}
    if navigation_extra:
        navigation.update(navigation_extra)
    project = {"communication": {"required": False}, "navigation": navigation,
               "surveillance": {"required": False}}
    if communication_required:
        project["communication"] = communication_requirement()
    value = {"project_default": project, "route_overrides": {}}
    if route_planning is not None:
        value["route_overrides"] = {"R1": {
            "communication": {"required": False},
            "navigation": {"required": True, "services": {RTK: {
                "required": True, "service_key": RTK, "planning": route_planning,
            }}},
            "surveillance": {"required": False},
        }}
    return normalize_required_cns(value)


def ready_planning(**extra):
    value = {
        "max_reference_baseline_m": TEST_BASELINE_M,
        "required_distinct_site_count": 1,
        "delivery_service_key": COMMUNICATION,
        "confirmed": True,
        "source": "测试显式输入：production 无此默认值",
    }
    value.update(extra)
    return value


def communication_facilities(
    *, longitude=122.0, latitude=30.0, radius_m=10000.0, count=1, independence=True,
):
    offsets = (0.0, 0.01, -0.01, 0.02)
    items = []
    for index in range(count):
        parameters = {"performance": {"coverage_radius_m": radius_m, "max_latency_s": 1.0}}
        if independence:
            parameters["independence_confirmed"] = True
            parameters["independence_group"] = f"CS-{index}"
        items.append({
            "facility_id": f"C-{index}", "site_id": f"CS-{index}", "name": f"C-{index}",
            "coordinate": [longitude + offsets[index], latitude], "coordinate_system": "wgs84",
            "status": "active",
            "vertical_profile": {"confirmed": True, "service_origin_egm2008_m": 10.0},
            "devices": [{
                "device_id": f"CD-{index}", "subsystem": "C", "service_key": COMMUNICATION,
                "status": "active",
                "type": {"technology": "lte", "interfaces": ["ethernet"]},
                "coverage_geometry": {"model": "hemisphere", "confirmed": True,
                                      "slant_range_m": radius_m},
                "service_model": {
                    "confirmed": True, "model_family": "declared_performance",
                    "parameters": parameters,
                },
                "vertical_profile": {"confirmed": True, "service_origin_egm2008_m": 10.0},
            }],
        })
    return {"items": items}


def corridor_policy():
    return normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": 0.0,
        "vertical_lower_margin_m": 5.0, "vertical_upper_margin_m": 5.0,
        "confirmed": True, "source": "test",
    }}})


#: 测试走廊中心（与测试站址坐标同一局部区域）。
CORRIDOR_LON = 122.0
CORRIDOR_LAT = 30.0


def route_and_grid():
    route = {"route_id": "R1", "status": "passed",
             "path": [[CORRIDOR_LON - 0.001, CORRIDOR_LAT], [CORRIDOR_LON + 0.001, CORRIDOR_LAT]]}
    grid = {"cells": [{
        "grid_id": "G1",
        "bbox": [CORRIDOR_LON - 0.0005, CORRIDOR_LAT,
                 CORRIDOR_LON + 0.0005, CORRIDOR_LAT + 0.001],
    }]}
    grid_attributes = {"terrain": {"status": "passed", "cells": {
        "G1": {"status": "passed", "surface_class": "land", "surface_elevation_mean_m": 0.0},
    }}}
    return route, grid, grid_attributes


def spatial_3d():
    return {
        "altitude_layers": [{
            "altitude_layer_id": "ALT-080", "lower_altitude_m": 70.0,
            "upper_altitude_m": 90.0, "vertical_reference": "egm2008_orthometric",
            "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant",
            "vertical_reference": "egm2008_orthometric", "constant_altitude_m": 80.0,
            "waypoints": [], "confirmed": True, "status": "confirmed",
            "geoid_undulation_m": None,
        }},
        "site_vertical_profiles": {},
    }


def coverage_facilities(*, count=0, radius_m=2000.0, longitude=122.0, latitude=30.0):
    return {"items": [{
        "facility_id": f"GCS-{index}", "site_id": f"GCS-{index}", "name": f"GCS-{index}",
        "coordinate": [longitude + index * 0.01, latitude], "coordinate_system": "wgs84",
        "status": "active",
        "vertical_profile": {"confirmed": True, "service_origin_egm2008_m": 10.0},
        "devices": [],
    } for index in range(count)]}


def grounded_required(*, planning=None, communication_required=False):
    """`ground/navigation` legacy 要求（无显式 services map）。"""

    project = {
        "communication": {"required": False},
        "navigation": {"required": True, "type": {"technology": "gnss_rtk"}},
        "surveillance": {"required": False},
    }
    if communication_required:
        project["communication"] = communication_requirement()
    return normalize_required_cns({"project_default": project, "route_overrides": {}})

def aircraft_profile():
    """测试用机载能力（**只**用于让 canonical C 服务判定可运行，不是 Round C 规划对象）。

    ``N`` 刻意声明为 ``gnss_rtk``：这正是"机载 RTK ≠ 地面 RTK 规划需求"的对照。
    """

    return {
        "aircraft_id": "TEST-AC",
        "name": "TEST-AC",
        "status": "confirmed", "confirmed": True,
        "communication": {
            "type": {"technology": "4g", "interfaces": ["ethernet"]},
            "performance": {"coverage_radius_m": 10000, "max_latency_s": 1.0},
            "confirmed": True, "status": "confirmed", "capabilities": ["service_model"],
        },
        "navigation": {
            "type": {"technology": "gnss_rtk"},
            "performance": {"accuracy_m": 1.0},
            "confirmed": True, "status": "confirmed", "capabilities": ["service_model"],
        },
        "surveillance": {"confirmed": False, "status": "pending_confirmation"},
    }


def run_p14(required_cns, *, navigation_evidence, facilities=None, aircraft=None):
    route, grid, grid_attributes = route_and_grid()
    return CNSServiceCorridorV1().evaluate(
        [route], spatial_3d(), grid, grid_attributes, required_cns,
        aircraft if aircraft is not None else aircraft_profile(),
        facilities or {"items": []}, {"items": []}, corridor_policy(),
        navigation_service_evidence=navigation_evidence,
    )


def navigation_entry(corridor):
    voxel = corridor["routes"][0]["voxels"][0]
    entry = next(item for item in voxel["subsystems"] if item["subsystem"] == "N")
    return next(
        item for item in entry.get("service_redundancy") or []
        if item.get("service_key") == RTK
    )


def p15(corridor, required_cns):
    return CNSCorridorGapAnalyzerV1().evaluate(corridor, required_cns, {})


def navigation_service_entry(gap):
    voxel = gap["routes"][0]["voxels"][0]
    entry = next(item for item in voxel["subsystems"] if item["subsystem"] == "N")
    return next(item for item in entry.get("services") or [] if item.get("service_key") == RTK)


def minimal_corridor(*, voxels):
    return {
        "status": "passed", "algorithm_id": "cns_service_corridor_v1",
        "algorithm_version": "1.0", "input_fingerprint": "p14",
        "routes": [{
            "route_id": "R1", "route_length_m": 100.0, "status": "failed",
            "voxels": voxels, "subsystems": [],
        }],
    }


def navigation_voxel(entry, *, voxel_id="V1", volume=10.0):
    return {
        "voxel_id": voxel_id, "grid_id": "G1", "altitude_layer_id": "ALT-080",
        "nearest_route_offset_m": 0.0, "cell_half_diagonal_m": 5.0,
        "discretized_volume_proxy_m3": volume, "surface_class": "land",
        "subsystems": [{
            "subsystem": "N", "planning_status": entry.get("status"),
            "p8_status": "unknown", "provider_evaluations": [],
            "service_redundancy": [entry], "reasons": [], "evidence": [],
        }],
    }


def site_evidence(*, existing=None, candidates=None, towers=None):
    return build_navigation_service_evidence(
        required(planning=ready_planning()),
        route_ids=["R1"],
        existing_facilities=existing or {"items": []},
        candidate_sites=candidates or {"items": []},
        tower_colocation=towers or {"items": []},
    )


def planning_evidence(*, sites):
    """构造带 P16 建站候选（suitability 已确认、尚未建成）的 navigation evidence。"""

    return build_navigation_service_evidence(
        required(planning=ready_planning()),
        route_ids=["R1"],
        tower_colocation={"items": sites},
    )


def navigation_action_target(target_id=None):
    return {
        "service_key": RTK, "planner_family": PLANNER_FAMILY,
        "target_id": target_id or f"R1|V1|{RTK}", "route_id": "R1",
        "subsystem": "N", "target_scope": "service", "required_units": 1,
    }


def not_installed_tower(site_id, longitude, latitude, *, tower_id=None):
    """suitability 已确认、但**尚未建成**参考站的共塔候选（P16 建站候选）。"""

    site = tower_colocation_site(
        site_id, longitude, latitude, suitability_value=suitability(), tower_id=tower_id,
    )
    site["navigation_site_suitability"]["reference_station_installed"] = False
    return site


# ---------------------------------------------------------------------------
# A / B：legacy 与机载 gnss_rtk 绝不产生地面 RTK 规划需求
# ---------------------------------------------------------------------------


def test_a_legacy_navigation_without_explicit_service_has_no_rtk_evidence():
    legacy = grounded_required()
    assert build_navigation_service_evidence(
        legacy, route_ids=["R1"],
        tower_colocation={"items": [tower_colocation_site(
            "T1", 122.0, 30.0, suitability_value=suitability(),
        )]},
    ) is None
    corridor = run_p14(legacy, navigation_evidence=None)
    entry = next(item for item in corridor["routes"][0]["voxels"][0]["subsystems"]
                 if item["subsystem"] == "N")
    assert RTK not in [item.get("service_key") for item in entry.get("service_redundancy") or []]


def test_b_aircraft_gnss_rtk_never_creates_ground_reference_station_requirement():
    aircraft = aircraft_profile()
    centerline = grounded_required()
    evidence = build_navigation_service_evidence(
        centerline, route_ids=["R1"],
        tower_colocation={"items": [tower_colocation_site(
            "T1", 122.0, 30.0, suitability_value=suitability(),
        )]},
    )
    assert evidence is None
    corridor = run_p14(centerline, navigation_evidence=evidence, aircraft=aircraft)
    entry = next(item for item in corridor["routes"][0]["voxels"][0]["subsystems"]
                 if item["subsystem"] == "N")
    assert all(item.get("service_key") != RTK for item in entry.get("service_redundancy") or [])


# ---------------------------------------------------------------------------
# C / D / E / F：planning policy 未确认 / 缺参数 ⇒ unknown（绝不 deficit）
# ---------------------------------------------------------------------------


def test_c_planning_not_confirmed_is_unknown():
    evidence = build_navigation_service_evidence(
        required(planning=ready_planning(confirmed=False)), route_ids=["R1"],
        tower_colocation={"items": [tower_colocation_site(
            "T1", 122.0, 30.0, suitability_value=suitability(),
        )]},
    )
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.005, "latitude": 30.0},
    )
    assert entry["status"] == "unknown"
    assert entry["geometry_status"] == "unknown"
    assert REASON_POLICY_NOT_CONFIRMED in entry["reasons"]
    assert entry["distinct_site_count"] is None
    assert entry["gap_causes"] == []


def test_d_missing_max_reference_baseline_is_unknown():
    planning = ready_planning()
    planning.pop("max_reference_baseline_m")
    evidence = build_navigation_service_evidence(
        required(planning=planning), route_ids=["R1"],
        tower_colocation={"items": [tower_colocation_site(
            "T1", 122.0, 30.0, suitability_value=suitability(),
        )]},
    )
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.005, "latitude": 30.0},
    )
    assert entry["status"] == "unknown"
    assert REASON_BASELINE_MISSING in entry["reasons"]
    assert entry["max_reference_baseline_m"] is None


def test_e_missing_required_distinct_site_count_is_unknown():
    planning = ready_planning()
    planning.pop("required_distinct_site_count")
    evidence = build_navigation_service_evidence(
        required(planning=planning), route_ids=["R1"],
        tower_colocation={"items": [tower_colocation_site(
            "T1", 122.0, 30.0, suitability_value=suitability(),
        )]},
    )
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.005, "latitude": 30.0},
    )
    assert entry["status"] == "unknown"
    assert REASON_SITE_COUNT_MISSING in entry["reasons"]
    assert entry["required_distinct_site_count"] is None


def test_f_production_source_has_no_default_rtk_distance():
    root = Path(__file__).parents[1]
    text = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in (
            "cns_planner/domain/navigation_augmentation.py",
            "cns_planner/application/navigation_reference_station_planning_service.py",
        )
    )
    for forbidden in ("15000", "10000", "20000", "RTK_RADIUS", "DEFAULT_RTK_RADIUS",
                      "baseline_m =", "DEFAULT_BASELINE"):
        assert forbidden not in text
    # 测试里的 15000 只是显式测试输入。
    assert TEST_BASELINE_M == 15000


# ---------------------------------------------------------------------------
# G / H / I / J / K：station baseline 几何与 distinct-site 计数
# ---------------------------------------------------------------------------


def test_g_one_station_within_baseline_satisfies_required_one():
    evidence = site_evidence(towers={"items": [tower_colocation_site(
        "T1", 122.0, 30.0, suitability_value=suitability(),
    )]})
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.001, "latitude": 30.0},
        delivery={"service_key": COMMUNICATION, "status": "satisfied"},
    )
    assert entry["geometry_status"] == "satisfied"
    assert entry["status"] == "satisfied"
    assert entry["distinct_site_count"] == 1
    assert entry["required_distinct_site_count"] == 1
    assert entry["providers"][0]["baseline_distance_m"] == pytest.approx(95.4, abs=1.0)
    assert entry["gap_causes"] == []


def test_h_required_two_with_one_distinct_site_is_confirmed_deficit():
    evidence = site_evidence(towers={"items": [tower_colocation_site(
        "T1", 122.0, 30.0, suitability_value=suitability(),
    )]})
    evidence["routes"][0]["policy"]["required_distinct_site_count"] = 2
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.001, "latitude": 30.0},
        delivery={"service_key": COMMUNICATION, "status": "satisfied"},
    )
    assert entry["required_distinct_site_count"] == 2
    assert entry["distinct_site_count"] == 1
    assert entry["geometry_status"] == "confirmed_deficit"
    assert entry["status"] == "confirmed_deficit"
    assert entry["gap_causes"] == [CAUSE_REFERENCE_STATION_DEFICIT]


def test_i_two_navigation_units_on_one_physical_site_count_as_one():
    facility = existing_facility("F1", "S1", 122.0, 30.0, suitability_value=suitability())
    facility["devices"] = [
        {"device_id": "NAV-1", "subsystem": "N", "service_key": RTK},
        {"device_id": "NAV-2", "subsystem": "N", "service_key": RTK},
    ]
    providers, _, _ = collect_navigation_sites(existing_facilities={"items": [facility]})
    assert len(providers) == 1
    assert providers[0]["distinct_site_id"] == "site:S1"
    entry = evidence_for_probe(
        site_evidence(existing={"items": [facility]}), "R1",
        {"voxel_id": "V1", "longitude": 122.0, "latitude": 30.0},
    )
    assert entry["distinct_site_count"] == 1


def test_j_two_distinct_sites_count_as_two():
    evidence = site_evidence(towers={"items": [
        tower_colocation_site("T1", 122.0, 30.0, suitability_value=suitability()),
        tower_colocation_site("T2", 122.02, 30.0, suitability_value=suitability()),
    ]})
    evidence["routes"][0]["policy"]["required_distinct_site_count"] = 2
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.01, "latitude": 30.0},
        delivery={"service_key": COMMUNICATION, "status": "satisfied"},
    )
    assert entry["distinct_site_count"] == 2
    assert set(entry["distinct_site_ids"]) == {"tower:T1", "tower:T2"}
    assert entry["status"] == "satisfied"


def test_k_station_beyond_baseline_is_not_counted_as_provider():
    planning = ready_planning(max_reference_baseline_m=1000)
    evidence = build_navigation_service_evidence(
        required(planning=planning), route_ids=["R1"],
        tower_colocation={"items": [
            tower_colocation_site("NEAR", 122.001, 30.001, suitability_value=suitability()),
            tower_colocation_site("FAR", 122.5, 30.5, suitability_value=suitability()),
        ]},
    )
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.0, "latitude": 30.0},
        delivery={"service_key": COMMUNICATION, "status": "satisfied"},
    )
    assert [item["distinct_site_id"] for item in entry["providers"]] == ["tower:NEAR"]
    assert entry["distinct_site_count"] == 1
    assert entry["status"] == "satisfied"


# ---------------------------------------------------------------------------
# L / M / Q / R：Communication 依赖合成
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "geometry_status, delivery_status, expected_status, expected_causes, dependency_only",
    [
        ("satisfied", "satisfied", "satisfied", [], False),
        ("satisfied", "confirmed_deficit", "confirmed_deficit",
         [CAUSE_CORRECTION_DELIVERY_DEFICIT], True),
        ("satisfied", "unknown", "unknown", [], False),
        ("confirmed_deficit", "satisfied", "confirmed_deficit",
         [CAUSE_REFERENCE_STATION_DEFICIT], False),
        ("confirmed_deficit", "confirmed_deficit", "confirmed_deficit",
         [CAUSE_REFERENCE_STATION_DEFICIT, CAUSE_CORRECTION_DELIVERY_DEFICIT], False),
        ("confirmed_deficit", "unknown", "confirmed_deficit",
         [CAUSE_REFERENCE_STATION_DEFICIT], False),
    ],
)
def test_l_m_dependency_status_matrix(
    geometry_status, delivery_status, expected_status, expected_causes, dependency_only,
):
    evidence = site_evidence(towers={"items": [tower_colocation_site(
        "T1", 122.0, 30.0, suitability_value=suitability(),
    )]})
    evidence["routes"][0]["policy"]["required_distinct_site_count"] = (
        1 if geometry_status == "satisfied" else 2
    )
    delivery = (
        None if delivery_status == "unknown"
        else {"service_key": COMMUNICATION, "status": delivery_status}
    )
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.001, "latitude": 30.0},
        delivery=delivery,
    )
    assert entry["geometry_status"] == geometry_status
    assert entry["status"] == expected_status, entry["reasons"]
    assert entry["gap_causes"] == expected_causes
    assert entry["dependency_only"] is dependency_only


def test_m_communication_deficit_with_station_satisfied_only_asks_for_delivery():
    evidence = site_evidence(towers={"items": [tower_colocation_site(
        "T1", 122.0, 30.0, suitability_value=suitability(),
    )]})
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.001, "latitude": 30.0},
        delivery={"service_key": COMMUNICATION, "status": "confirmed_deficit"},
    )
    assert entry["status"] == "confirmed_deficit"
    assert entry["geometry_status"] == "satisfied"
    assert entry["dependency_only"] is True
    assert entry["recommended_dependency_service"] == COMMUNICATION


def test_q_unknown_communication_never_passes_navigation():
    evidence = site_evidence(towers={"items": [tower_colocation_site(
        "T1", 122.0, 30.0, suitability_value=suitability(),
    )]})
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.001, "latitude": 30.0},
        delivery=None,
    )
    assert entry["status"] == "unknown"
    assert entry["geometry_status"] == "satisfied"


def test_r_missing_delivery_service_key_is_unknown():
    planning = ready_planning()
    planning["delivery_service_key"] = None
    evidence = build_navigation_service_evidence(
        required(planning=planning), route_ids=["R1"],
        tower_colocation={"items": [tower_colocation_site(
            "T1", 122.0, 30.0, suitability_value=suitability(),
        )]},
    )
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.001, "latitude": 30.0},
        delivery={"service_key": COMMUNICATION, "status": "satisfied"},
    )
    assert entry["status"] == "unknown"
    assert REASON_DELIVERY_NOT_CONFIGURED in entry["reasons"]
    assert entry["delivery_status"] == "not_configured"


# ---------------------------------------------------------------------------
# S / T / U：site suitability fail-closed
# ---------------------------------------------------------------------------


def test_s_plain_tower_without_navigation_suitability_is_not_eligible():
    plain = tower_colocation_site("T1", 122.0, 30.0)
    providers, unconfirmed, candidates = collect_navigation_sites(
        tower_colocation={"items": [plain]},
    )
    assert providers == [] and unconfirmed == [] and candidates == []
    # 普通 tower 既不能作为已建成 provider，也不能成为 P16 建站候选。
    actions = navigation_reference_station_candidate_actions(
        [navigation_action_target()],
        navigation_evidence=build_navigation_service_evidence(
            required(planning=ready_planning()), route_ids=["R1"],
            tower_colocation={"items": [plain]},
        ),
    )
    assert actions == []
    entry = evidence_for_probe(
        site_evidence(towers={"items": [plain]}), "R1",
        {"voxel_id": "V1", "longitude": 122.0, "latitude": 30.0},
    )
    assert entry["distinct_site_count"] == 0
    assert entry["geometry_status"] == "confirmed_deficit"
    assert entry["status"] == "confirmed_deficit"


def test_t_confirmed_suitable_tower_is_a_baseline_provider_or_planning_candidate():
    installed = tower_colocation_site("T1", 122.0, 30.0, suitability_value=suitability())
    providers, unconfirmed, candidates = collect_navigation_sites(
        tower_colocation={"items": [installed]},
    )
    assert [item["distinct_site_id"] for item in providers] == ["tower:T1"]
    assert providers[0]["site_suitability"]["planning_use_confirmed"] is True
    assert unconfirmed == [] and candidates == []

    # 显式声明"尚未建成"时：不占基线 provider 池，但成为 P16 建站候选。
    planned = not_installed_tower("T2", 122.02, 30.0)
    providers, unconfirmed, candidates = collect_navigation_sites(
        tower_colocation={"items": [planned]},
    )
    assert providers == [] and unconfirmed == []
    assert [item["distinct_site_id"] for item in candidates] == ["tower:T2"]
    evidence = build_navigation_service_evidence(
        required(planning=ready_planning()), route_ids=["R1"],
        tower_colocation={"items": [planned]},
    )
    actions = navigation_reference_station_candidate_actions(
        [navigation_action_target()], navigation_evidence=evidence,
    )
    assert [item["distinct_site_id"] for item in actions] == ["tower:T2"]


def test_u_available_subsystems_n_alone_is_not_suitability():
    site = tower_colocation_site("T1", 122.0, 30.0)
    assert site["available_subsystems"] == ["N"]
    providers, _, candidates = collect_navigation_sites(tower_colocation={"items": [site]})
    assert providers == [] and candidates == []
    assert normalize_navigation_site_suitability(None) is None
    assert normalize_navigation_site_suitability({"confirmed": True})[
        "eligible_for_navigation_reference_station"
    ] is False


def test_unconfirmed_suitability_stays_unknown_not_confirmed_deficit():
    pending = tower_colocation_site(
        "T1", 122.0, 30.0, suitability_value=suitability(planning_use=False),
    )
    evidence = site_evidence(towers={"items": [pending]})
    assert evidence["providers"] == []
    assert [item["status"] for item in evidence["unconfirmed_providers"]] == [
        "unconfirmed_suitability",
    ]
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.0, "latitude": 30.0},
        delivery={"service_key": COMMUNICATION, "status": "satisfied"},
    )
    assert entry["status"] == "unknown"
    assert entry["geometry_status"] == "unknown"


# ---------------------------------------------------------------------------
# P14 / P15 表达
# ---------------------------------------------------------------------------


def test_p14_only_explicit_rtk_service_produces_evidence():
    explicit = required(planning=ready_planning())
    evidence = site_evidence(towers={"items": [tower_colocation_site(
        "T1", 122.0, 30.0, suitability_value=suitability(),
    )]})
    corridor = run_p14(explicit, navigation_evidence=evidence)
    entry = navigation_entry(corridor)
    assert entry["service_key"] == RTK
    assert entry["voxel_id"]
    assert entry["planner_family"] == PLANNER_FAMILY
    assert entry["supports_site_planning"] is True
    assert entry["planning_unit"] == REFERENCE_STATION_PLANNING_UNIT
    assert entry["device_id"] is None
    n_subsystem = next(item for item in corridor["routes"][0]["voxels"][0]["subsystems"]
                       if item["subsystem"] == "N")
    assert n_subsystem["planning_status"] == entry["status"]

    legacy = grounded_required()
    legacy_corridor = run_p14(legacy, navigation_evidence=None)
    legacy_n = next(item for item in legacy_corridor["routes"][0]["voxels"][0]["subsystems"]
                    if item["subsystem"] == "N")
    assert "service_redundancy" not in legacy_n or all(
        item.get("service_key") != RTK for item in legacy_n.get("service_redundancy") or []
    )


def test_p15_summarizes_navigation_service_independently():
    # 前提：Navigation augmentation 依赖 C:communication，因此测试里的通信要求必须
    # 已确认且完整，否则 delivery 只能是 unknown（见下一个测试）。
    planning = ready_planning(required_distinct_site_count=2)
    explicit = required(planning=planning, communication_required=True)
    evidence = site_evidence(towers={"items": [tower_colocation_site(
        "T1", 122.0, 30.0, suitability_value=suitability(),
    )]})
    evidence["routes"][0]["policy"]["required_distinct_site_count"] = 2
    # C:communication 要求 2 个独立物理站址：两个 confirmed 通信站 ⇒ delivery satisfied。
    corridor = run_p14(
        explicit, navigation_evidence=evidence,
        facilities=communication_facilities(count=2),
    )
    gap = p15(corridor, explicit)
    n_summary = next(item for item in gap["routes"][0]["subsystems"]
                     if item["subsystem"] == "N")
    bucket = next(item for item in n_summary["service_redundancy"]
                  if item["service_key"] == RTK)
    assert bucket["voxel_count"] == 1
    assert bucket["status"] == "confirmed_deficit"
    assert bucket["gap_cause_counts"] == {CAUSE_REFERENCE_STATION_DEFICIT: 1}
    assert bucket["geometry_status_counts"]["confirmed_deficit"] == 1
    assert bucket["delivery_status_counts"] == {"satisfied": 1}
    assert bucket["required_distinct_site_count"] == 2
    assert bucket["distinct_site_count"] == 1
    assert bucket["dependency_only"] is False
    assert bucket["recommended_dependency_service"] is None
    assert bucket["confirmed_target_voxel_ids"]
    assert bucket["station_deficit_voxel_ids"] == bucket["confirmed_target_voxel_ids"]
    assert bucket["unknown_voxel_ids"] == []
    # 站址 satisfied + delivery 满足 ⇒ N augmentation 整体 satisfied。
    satisfied_corridor = run_p14(
        explicit,
        navigation_evidence=site_evidence(existing={"items": [existing_facility(
            "F1", "S1", 122.0, 30.0, suitability_value=suitability(),
        )]}),
        facilities=communication_facilities(count=2),
    )
    satisfied_bucket = next(
        item for item in p15(satisfied_corridor, explicit)["routes"][0]["subsystems"]
        if item["subsystem"] == "N"
    )["service_redundancy"]
    assert next(item for item in satisfied_bucket if item["service_key"] == RTK)["status"] == "satisfied"


def test_p15_keeps_unknown_delivery_evidence_alongside_station_deficit():
    """已知站址缺口优先于未知，但 Communication 的 unknown 证据必须保留。"""

    planning = ready_planning(required_distinct_site_count=2)
    explicit = required(planning=planning, communication_required=True)
    evidence = site_evidence(towers={"items": [tower_colocation_site(
        "T1", 122.0, 30.0, suitability_value=suitability(),
    )]})
    evidence["routes"][0]["policy"]["required_distinct_site_count"] = 2
    # 通信子系统没有任何已确认 provider 证据 ⇒ C:communication = unknown（不是 deficit）。
    corridor = run_p14(explicit, navigation_evidence=evidence)
    bucket = next(
        item for item in p15(corridor, explicit)["routes"][0]["subsystems"]
        if item["subsystem"] == "N"
    )["service_redundancy"]
    rtk = next(item for item in bucket if item["service_key"] == RTK)
    assert rtk["status"] == "confirmed_deficit"
    assert rtk["gap_cause_counts"] == {CAUSE_REFERENCE_STATION_DEFICIT: 1}
    assert rtk["delivery_status_counts"] == {"unknown": 1}
    assert rtk["evidence_required_reasons"] == ["correction_delivery_unknown"]


def test_p15_keeps_both_deficit_causes_when_station_and_delivery_both_fail():
    """站址与交付同时已确认缺口 ⇒ 两个 cause 都必须保留。"""

    planning = ready_planning(required_distinct_site_count=2)
    explicit = required(planning=planning, communication_required=True)
    evidence = site_evidence(towers={"items": [tower_colocation_site(
        "T1", 122.0, 30.0, suitability_value=suitability(),
    )]})
    evidence["routes"][0]["policy"]["required_distinct_site_count"] = 2
    corridor = run_p14(
        explicit, navigation_evidence=evidence, facilities=communication_facilities(count=1),
    )
    bucket = next(
        item for item in p15(corridor, explicit)["routes"][0]["subsystems"]
        if item["subsystem"] == "N"
    )["service_redundancy"]
    rtk = next(item for item in bucket if item["service_key"] == RTK)
    assert rtk["status"] == "confirmed_deficit"
    assert rtk["gap_cause_counts"] == {
        CAUSE_REFERENCE_STATION_DEFICIT: 1, CAUSE_CORRECTION_DELIVERY_DEFICIT: 1,
    }


def test_p15_confirmed_target_id_is_route_voxel_service_key():
    planning = ready_planning(required_distinct_site_count=2)
    explicit = required(planning=planning, communication_required=True)
    evidence = site_evidence(towers={"items": [tower_colocation_site(
        "T1", 122.0, 30.0, suitability_value=suitability(),
    )]})
    evidence["routes"][0]["policy"]["required_distinct_site_count"] = 2
    corridor = run_p14(
        explicit, navigation_evidence=evidence, facilities=communication_facilities(count=2),
    )
    gap = p15(corridor, explicit)
    n_summary = next(item for item in gap["routes"][0]["subsystems"]
                     if item["subsystem"] == "N")
    bucket = next(item for item in n_summary["service_redundancy"]
                  if item["service_key"] == RTK)
    assert bucket["confirmed_target_voxel_ids"]
    assert bucket["station_deficit_voxel_ids"]
    targets, unknown = _targets(gap)
    rtk_targets = [item for item in targets if item.get("service_key") == RTK]
    assert [item["target_id"] for item in rtk_targets] == [
        f"R1|{bucket['confirmed_target_voxel_ids'][0]}|{RTK}"
    ]
    assert rtk_targets[0]["planner_family"] == PLANNER_FAMILY
    assert rtk_targets[0]["delivery_resolved"] is True
    assert not [item for item in unknown if item.get("service_key") == RTK]


# ---------------------------------------------------------------------------
# N / O：P16 是否生成导航站 action
# ---------------------------------------------------------------------------


def navigation_gap(*, geometry_status, delivery_status, required_count=1):
    entry = {
        "service_key": RTK, "subsystem": "N", "surface_dependent": False,
        "supports_site_planning": True, "status": "confirmed_deficit",
        "voxel_id": "V1", "geometry_status": geometry_status,
        "delivery_status": delivery_status,
        "gap_causes": (
            [CAUSE_REFERENCE_STATION_DEFICIT]
            if geometry_status == "confirmed_deficit" else []
        ) + (
            [CAUSE_CORRECTION_DELIVERY_DEFICIT]
            if delivery_status == "confirmed_deficit" else []
        ),
        "required_distinct_site_count": required_count, "distinct_site_count": 0,
        "max_reference_baseline_m": TEST_BASELINE_M,
        "delivery_service_key": COMMUNICATION,
        "distinct_site_ids": [], "reasons": [], "counting_basis": "distinct_site_id",
    }
    return CNSCorridorGapAnalyzerV1().evaluate(
        minimal_corridor(voxels=[navigation_voxel(entry)]),
        required(planning=ready_planning()), {},
    )


def test_n_pure_correction_delivery_deficit_never_produces_navigation_site_action():
    gap = navigation_gap(geometry_status="satisfied", delivery_status="confirmed_deficit")
    targets, unknown = _targets(gap)
    assert not [item for item in targets if item.get("service_key") == RTK]
    rtk_unknown = [item for item in unknown if item.get("service_key") == RTK]
    assert rtk_unknown and rtk_unknown[0]["dependency_only"] is True
    assert rtk_unknown[0]["recommended_dependency_service"] == COMMUNICATION
    actions = navigation_reference_station_candidate_actions(
        targets + rtk_unknown,
        navigation_evidence=planning_evidence(sites=[not_installed_tower("T1", 122.0, 30.0)]),
    )
    assert actions == []


def test_n2_dependency_only_target_never_produces_navigation_site_action():
    """即使把 dependency_only 条目当成 target 传入，导航侧也必须拒绝建站。"""

    dependency_only = {
        "service_key": RTK, "planner_family": PLANNER_FAMILY,
        "target_id": f"R1|V1|{RTK}", "route_id": "R1", "subsystem": "N",
        "target_scope": "service", "dependency_only": True,
        "required_units": 1, "current_units": 1, "remaining_units": 0,
    }
    assert navigation_reference_station_candidate_actions(
        [dependency_only],
        navigation_evidence=planning_evidence(sites=[not_installed_tower("T1", 122.0, 30.0)]),
    ) == []


def test_o_station_deficit_with_satisfied_delivery_produces_navigation_site_action():
    gap = navigation_gap(geometry_status="confirmed_deficit", delivery_status="satisfied")
    targets, unknown = _targets(gap)
    rtk_targets = [item for item in targets if item.get("service_key") == RTK]
    assert rtk_targets and not [item for item in unknown if item.get("service_key") == RTK]
    actions = navigation_reference_station_candidate_actions(
        rtk_targets,
        navigation_evidence=planning_evidence(sites=[
            not_installed_tower("T1", 122.0, 30.0, tower_id="TOWER-1"),
        ]),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action_type"] == ACTION_TYPE_ADD_REFERENCE_STATION
    assert action["planner_family"] == PLANNER_FAMILY
    assert action["reuse_class"] == "tower_colocation_host"


def test_p_both_deficits_keep_delivery_unresolved_after_adding_station():
    gap = navigation_gap(
        geometry_status="confirmed_deficit", delivery_status="confirmed_deficit",
    )
    targets, unknown = _targets(gap)
    rtk_targets = [item for item in targets if item.get("service_key") == RTK]
    assert rtk_targets
    assert rtk_targets[0]["delivery_resolved"] is False
    baseline = planning_evidence(sites=[
        not_installed_tower("T1", 122.0, 30.0),
        not_installed_tower("T2", 122.02, 30.0),
    ])
    actions = navigation_reference_station_candidate_actions(
        rtk_targets, navigation_evidence=baseline,
    )
    assert len(actions) == 2
    _, hypothetical = hypothetical_navigation_sites(baseline, actions)
    entry = evidence_for_probe(
        hypothetical, "R1", {"voxel_id": "V1", "longitude": 122.01, "latitude": 30.0},
        delivery={"service_key": COMMUNICATION, "status": "confirmed_deficit"},
    )
    # 站址缺口被补上，但 correction delivery 仍然是已确认缺口。
    assert entry["geometry_status"] == "satisfied"
    assert entry["status"] == "confirmed_deficit"
    assert entry["gap_causes"] == [CAUSE_CORRECTION_DELIVERY_DEFICIT]


# ---------------------------------------------------------------------------
# P16 端到端：导航站选址 + hypothetical rerun
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, state):
        self.state = state
        self.saves = 0

    def save(self):
        self.saves += 1


class _Invalidation:
    def __init__(self):
        self.calls = []

    def cns_plan_review(self, reason="review_baseline_changed"):
        self.calls.append(("cns_plan_review", reason))


def p16_state(
    *, required_cns, tower_sites=None, existing_sites=None,
    communication_count=2, candidate_sites=None,
):
    route, grid, grid_attributes = route_and_grid()
    towers = deepcopy(tower_sites) if tower_sites is not None else {"items": []}
    for site in towers.get("items") or []:
        # 共塔候选在**基线**里是"获准安装但尚未建成"：suitability 已确认，但还没有
        # 建成参考站，因此不进入基线 provider 池 —— 它才是 P16 要补的建站候选。
        value = site.get("navigation_site_suitability")
        if isinstance(value, dict):
            value["reference_station_installed"] = False
    state = {
        "required_cns": required_cns,
        "operational_routes": [route],
        "spatial_3d": spatial_3d(),
        "grid": grid,
        "grid_attributes": grid_attributes,
        "aircraft_profiles": {"items": [normalize_aircraft_profile(aircraft_profile())]},
        "selected_aircraft_profile_id": "TEST-AC",
        "algorithm_selection": {},
        "existing_cns_facilities": {
            "items": list(existing_sites or []) + communication_facilities(
                count=communication_count,
            )["items"],
        },
        "candidate_sites": candidate_sites or {"items": []},
        "tower_colocation_candidates": towers,
        "device_catalog": {"items": []},
        "cns_corridor_policy": corridor_policy(),
        "cns_corridor_assessment": {},
        "cns_corridor_gap_assessment": {},
        "result_statuses": {},
    }
    return state


def run_p16(state):
    from cns_planner.algorithms.corridor.v1 import CNSServiceCorridorV1
    from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
    from cns_planner.application.corridor_service import CNSCorridorService
    from cns_planner.application.corridor_site_planning_service import (
        CorridorSitePlanningService,
    )
    from cns_planner.site_planner.corridor_reuse_first_v2 import (
        CorridorReuseFirstSitePlannerV2,
    )

    session = _Session(state)
    invalidation = _Invalidation()
    corridor_service = CNSCorridorService(
        session, CNSServiceCorridorV1(), invalidation, lambda: state,
    )
    corridor = corridor_service.compute(corridor_service.compute_input())
    state["cns_corridor_assessment"] = corridor
    gap = CNSCorridorGapAnalyzerV1().evaluate(
        corridor, state["required_cns"], {},
    )
    state["cns_corridor_gap_assessment"] = gap
    service = CorridorSitePlanningService(
        session, CorridorReuseFirstSitePlannerV2(), CNSServiceCorridorV1(),
        CNSCorridorGapAnalyzerV1(), invalidation, lambda: state,
    )
    service.evaluate({})
    return state, state["cns_corridor_site_plan"]


def only_navigation_actions(result):
    return [
        item for item in result.get("selected_actions") or []
        if item.get("planner_family") == PLANNER_FAMILY
    ]


def test_p16_end_to_end_adds_navigation_reference_station_and_reruns_chain():
    planning = ready_planning(required_distinct_site_count=2)
    state = p16_state(
        required_cns=required(planning=planning, communication_required=True),
        # NAV-1 是已确认的现存地面导航增强站；TOWER-2 是尚未使用的已确认共塔候选。
        existing_sites=[existing_facility(
            "NAV-1", "NAVS-1", 122.0, 30.0, suitability_value=suitability(),
        )],
        tower_sites={"items": [
            tower_colocation_site(
                "T2", 122.02, 30.0, suitability_value=suitability(), tower_id="TOWER-2",
            ),
        ]},
        communication_count=2,
    )
    before_formal = deepcopy(state["existing_cns_facilities"])
    state, result = run_p16(state)
    actions = only_navigation_actions(result)
    assert actions, result.get("candidate_actions")
    action = actions[0]
    assert action["action_type"] == ACTION_TYPE_ADD_REFERENCE_STATION
    assert action["device_id"] is None
    assert action["planning_unit"] == REFERENCE_STATION_PLANNING_UNIT
    assert action["equipment_selection_status"] == "not_selected"
    assert action["reuse_class"] == "tower_colocation_host"
    assert action["distinct_site_id"] == "tower:TOWER-2"
    assert action["impact"]["confirmed_requirement_unit_volume_gain"] > 0
    # 正式 ExistingCNS 绝不被写入；假想结果标记 persisted_as_upstream = false。
    assert state["existing_cns_facilities"] == before_formal
    assert result["final_hypothetical_evidence"]["persisted_as_upstream"] is False
    # 假想重跑后的 P14/P15 里，站址缺口已被补上。
    hypothetical = result["final_hypothetical_evidence"]["corridor_gap"]
    bucket = next(
        item for item in hypothetical["routes"][0]["subsystems"]
        if item["subsystem"] == "N"
    )["service_redundancy"]
    rtk = next(item for item in bucket if item["service_key"] == RTK)
    assert rtk["status"] == "satisfied"
    assert rtk["distinct_site_count"] == 2


def test_p16_never_adds_navigation_station_for_pure_communication_deficit():
    # 站址 required=1 且已有 1 个 confirmed 导航站 ⇒ 只剩 correction delivery 缺口。
    state = p16_state(
        required_cns=required(planning=ready_planning(), communication_required=True),
        existing_sites=[existing_facility(
            "NAV-1", "NAVS-1", 122.0, 30.0, suitability_value=suitability(),
        )],
        tower_sites={"items": [tower_colocation_site(
            "T2", 122.02, 30.0, suitability_value=suitability(), tower_id="TOWER-2",
        )]},
        communication_count=1,
    )
    state, result = run_p16(state)
    assert only_navigation_actions(result) == []
    # 依赖缺口如实记录，并把建设动作指向 Communication planner。
    dependency = [
        item for item in result.get("unknown_evidence_required") or []
        if item.get("service_key") == RTK
    ]
    assert dependency and dependency[0]["dependency_only"] is True
    assert dependency[0]["recommended_dependency_service"] == COMMUNICATION
    # 该 voxel 也没有被登记成导航建站 target。
    assert not [item for item in result.get("targets") or []
                if item.get("service_key") == RTK]


def test_p16_navigation_action_never_enters_formal_facilities_or_device_catalog():
    planning = ready_planning(required_distinct_site_count=2)
    state = p16_state(
        required_cns=required(planning=planning, communication_required=True),
        existing_sites=[existing_facility(
            "NAV-1", "NAVS-1", 122.0, 30.0, suitability_value=suitability(),
        )],
        tower_sites={"items": [
            tower_colocation_site(
                "T2", 122.02, 30.0, suitability_value=suitability(), tower_id="TOWER-2",
            ),
        ]},
        communication_count=2,
    )
    state, result = run_p16(state)
    devices = [
        device.get("device_id")
        for facility in state["existing_cns_facilities"]["items"]
        for device in facility.get("devices") or []
    ]
    assert not [value for value in devices if value and str(value).startswith("p16-proposal")]
    for action in only_navigation_actions(result):
        assert action["persisted_as_upstream"] is False
        assert action["device_id"] is None
        assert not str(action["action_id"]).startswith("p16-proposal")


# ---------------------------------------------------------------------------
# V / W：hypothetical 不写正式 ExistingCNS；selected action 不伪装真实设备
# ---------------------------------------------------------------------------


def test_v_hypothetical_station_never_writes_formal_existing_cns():
    formal = {"items": [existing_facility("F1", "S1", 122.0, 30.0)], "count": 1}
    before = deepcopy(formal)
    baseline = planning_evidence(sites=[not_installed_tower("T1", 122.01, 30.0)])
    action = navigation_reference_station_candidate_actions(
        [navigation_action_target()], navigation_evidence=baseline,
    )[0]
    facilities, _ = hypothetical_navigation_sites(baseline, [action])
    assert formal == before
    assert action["persisted_as_upstream"] is False
    assert facilities["items"][0]["metadata"]["navigation_reference_station_provider"] is True


def test_w_selected_action_has_no_device_id_and_correct_planning_unit():
    baseline = planning_evidence(sites=[not_installed_tower("T1", 122.0, 30.0)])
    action = navigation_reference_station_candidate_actions(
        [navigation_action_target()], navigation_evidence=baseline,
    )[0]
    assert action["device_id"] is None
    assert action["equipment_selection_status"] == EQUIPMENT_SELECTION_STATUS == "not_selected"
    assert action["planning_unit"] == REFERENCE_STATION_PLANNING_UNIT
    assert action["maturity"] == PLANNING_UNIT_MATURITY
    assert action["service_key"] == RTK
    assert "manufacturer" not in action and "model" not in action


# ---------------------------------------------------------------------------
# X：route override 单独设置 RTK policy
# ---------------------------------------------------------------------------


def test_x_route_override_can_configure_rtk_policy_independently():
    route_planning = ready_planning(max_reference_baseline_m=2000, required_distinct_site_count=1)
    value = required(planning=None, route_planning=route_planning)
    evidence = build_navigation_service_evidence(
        value, route_ids=["R1"],
        tower_colocation={"items": [tower_colocation_site(
            "NEAR", 122.001, 30.0, suitability_value=suitability(),
        ), tower_colocation_site(
            "FAR", 122.2, 30.0, suitability_value=suitability(),
        )]},
    )
    entry = evidence_for_probe(
        evidence, "R1", {"voxel_id": "V1", "longitude": 122.0, "latitude": 30.0},
        delivery={"service_key": COMMUNICATION, "status": "satisfied"},
    )
    assert entry["max_reference_baseline_m"] == 2000.0
    assert [item["distinct_site_id"] for item in entry["providers"]] == ["tower:NEAR"]


# ---------------------------------------------------------------------------
# Y / Z：invalidation
# ---------------------------------------------------------------------------


def test_y_navigation_policy_change_does_not_stale_theta_risk_validation_or_radar():
    protected = {
        "routes", "grid_risk", "grid_risk_v2", "route_risk_profiles",
        "layered_route_validations", "layered_operational_adoptions",
        "radar_surveillance_layout",
    }
    assert protected.isdisjoint(DEPENDENTS["required_cns"])
    assert {"cns_corridor_assessment", "cns_corridor_gap_assessment",
            "cns_corridor_site_plan", "report"} <= set(DEPENDENTS["required_cns"])


def test_z_communication_change_stales_navigation_downstream_but_not_radar():
    from cns_planner.application.invalidation_service import InvalidationService

    class Session:
        def __init__(self):
            self.state = {
                "required_cns": required(planning=ready_planning()),
                "result_statuses": {},
                "cns_corridor_assessment": {"status": "passed", "input_fingerprint": "a"},
                "cns_corridor_gap_assessment": {"status": "passed", "input_fingerprint": "b"},
                "cns_corridor_site_plan": {"status": "proposal_ready", "input_fingerprint": "c"},
                "radar_surveillance_layout": {"status": "passed"},
                "routes": {"status": "passed"},
                "route_risk_profiles": {"status": "passed"},
                "layered_route_validations": {"status": "passed"},
            }

        def save(self):
            pass

    session = Session()
    service = InvalidationService(session)
    service.cns_corridor()
    state = session.state
    assert state["cns_corridor_assessment"]["status"] == "stale"
    assert state["cns_corridor_gap_assessment"]["status"] == "stale"
    assert state["cns_corridor_site_plan"]["status"] == "stale"
    assert state["radar_surveillance_layout"]["status"] == "passed"
    assert state["routes"]["status"] == "passed"
    assert state["route_risk_profiles"]["status"] == "passed"
    assert state["layered_route_validations"]["status"] == "passed"


# ---------------------------------------------------------------------------
# source guards
# ---------------------------------------------------------------------------


def test_source_guards_forbid_hardcoded_rtk_distance_and_foreign_geometry():
    root = Path(__file__).parents[1]
    text = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in (
            "cns_planner/domain/navigation_augmentation.py",
            "cns_planner/application/navigation_reference_station_planning_service.py",
            "cns_planner/application/corridor_site_planning_service.py",
        )
    )
    for forbidden in (
        "RTK_RADIUS", "DEFAULT_RTK_RADIUS", "RID_RADIUS_BY_SURFACE",
        "COMMUNICATION_RADIUS_BY_SURFACE", "15000", "10000", "20000",
        "def haversine", "haversine(",
    ):
        assert forbidden not in text
    assert "distance_m" in (root / "cns_planner/domain/navigation_augmentation.py").read_text(
        encoding="utf-8",
    )


def test_navigation_target_never_enters_ordinary_site_candidate_cartesian_product():
    target = {"subsystem": "N", "service_key": RTK}
    catalog = {"items": [{
        "device_id": "NAV", "subsystem": "N", "service_key": RTK, "enabled": True,
    }]}
    assert candidate_actions([target], {"items": []}, {"items": []}, catalog) == []
    assert planner_family_for(RTK) == PLANNER_FAMILY


def test_navigation_requirement_helper_ignores_legacy_navigation_identity():
    legacy = grounded_required()
    assert build_navigation_service_evidence(legacy, route_ids=["R1"]) is None
    explicit = required(planning=ready_planning())
    assert build_navigation_service_evidence(explicit, route_ids=["R1"]) is not None
    assert SERVICE_KEY_NAVIGATION != RTK
    assert distinct_site_id_for({"site_id": "S1"}) == "site:S1"
