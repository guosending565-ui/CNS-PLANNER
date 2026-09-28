"""Round 1 P0：Communication / RID（合作监视）service contract、surface 动态范围与站址计数。

本文件只覆盖本轮新增的**纯算法/domain 契约**：

* A. RID 动态范围（land 2000 / coastal_uncertain 2000 / sea 5000 / unknown fail-closed）
* B. Communication 范围（land / coastal_uncertain / sea 均为 4000）
* C. ``evaluate_geometry_point``（list）与 ``GeometricProviderIndex``（index）逐项一致
* D/E. Communication / RID 的 distinct-site 计数（含 coast 按 land、sea 单站）
* F. service 隔离（Communication 与 RID 绝不互相充数）
* G. 同一物理站址上的多 subsystem（各 service 池各自为 1）
* H. 无物理身份时 fail-closed（绝不把 device_id 当站址）
* 附加：P7→P8 与 P15 的最小调用链接线、not_evaluated 声明、urban 只作设备事实
"""

from __future__ import annotations

import math

import pytest

from cns_planner.algorithms.coverage.geometric_3d import (
    GeometricProviderIndex, build_geometric_providers, evaluate_geometry_point,
)
from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from cns_planner.algorithms.service_capability.v1 import (
    distinct_site_provider_count, evaluate_capability_point,
    summarize_service_redundancy,
)
from cns_planner.domain.cns_service_contract import (
    COMMUNICATION_NOT_EVALUATED, COMMUNICATION_RADIUS_BY_SURFACE,
    RID_NOT_EVALUATED, RID_RADIUS_BY_SURFACE, RID_URBAN_ENABLED, RID_URBAN_RADIUS_M,
    SERVICE_KEY_COMMUNICATION, SERVICE_KEY_RID_COOPERATIVE, SURFACE_CLASSES,
    distinct_site_id_for, effective_radius_m, index_max_range_m,
    required_distinct_site_count, service_contract_for, service_key_for,
)
from cns_planner.domain.cns_inputs import normalize_coverage_geometry, normalize_device
from cns_planner.domain.geodesy import distance_m


EARTH_RADIUS_M = 6_371_008.8


def longitude_offset(metres, latitude=0.0):
    """把米制水平距离换算成纬度 ``latitude`` 处的经度差（供探针构造）。"""

    return math.degrees(metres / (EARTH_RADIUS_M * math.cos(math.radians(latitude))))


def sample(*, longitude=0.0, latitude=0.0, surface_class="land", altitude=100.0, vertical_status="passed"):
    return {
        "longitude": longitude, "latitude": latitude,
        "altitude_egm2008_m": altitude, "vertical_status": vertical_status,
        "surface_class": surface_class,
    }


def geometry_block(radius_by_surface, *, model="hemisphere"):
    block = {"model": model, "model_scope": "geometric_only",
             "source": "test", "confirmed": True, "status": "confirmed"}
    if radius_by_surface is not None:
        block["radius_by_surface"] = dict(radius_by_surface)
        block["slant_range_m"] = None
    return block


def provider(*, service_key, coordinate=(0.0, 0.0), origin=100.0, site="site:SITE-A",
             radius_by_surface=None, subsystem=None, device_id="D1", facility_id="F1",
             legacy_radius=None, surface_dependent=True):
    """直接构造 P7 provider（与 ``build_geometric_providers`` 的输出形状一致）。"""

    block = geometry_block(radius_by_surface)
    if legacy_radius is not None:
        block["slant_range_m"] = float(legacy_radius)
        block.pop("radius_by_surface", None)
    return {
        "facility_id": facility_id, "device_id": device_id,
        "coordinate": list(coordinate), "service_origin_egm2008_m": float(origin),
        "coverage_geometry": block,
        "subsystem": subsystem or str(service_key).split(":", 1)[0],
        "service_key": service_key, "service_surface_dependent": surface_dependent,
        "distinct_site_id": site,
    }


def rid_provider(**overrides):
    return provider(
        service_key=SERVICE_KEY_RID_COOPERATIVE,
        radius_by_surface=RID_RADIUS_BY_SURFACE, **overrides,
    )


def communication_provider(**overrides):
    return provider(
        service_key=SERVICE_KEY_COMMUNICATION,
        radius_by_surface=COMMUNICATION_RADIUS_BY_SURFACE, **overrides,
    )


def provider_evaluation(*, service_key, site, status="meets_under_model", device_id="D1", facility_id="F1"):
    return {
        "facility_id": facility_id, "device_id": device_id, "status": status,
        "subsystem": str(service_key).split(":", 1)[0], "service_key": service_key,
        "service_surface_dependent": True, "distinct_site_id": site,
        "stage": "provider_service",
    }


REQUIRED_BY_SURFACE = {
    "required": True, "status": "passed",
    "redundancy_by_surface": {"land": 2, "coastal_uncertain": 2, "sea": 1},
}


def service_entry(entries, service_key):
    matches = [item for item in entries if item["service_key"] == service_key]
    assert len(matches) == 1, f"expected exactly one {service_key} pool, got {matches}"
    return matches[0]


# ---------------------------------------------------------------------------
# A. RID 动态范围
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface_class,expected_covered,expected_status", [
    ("land", False, "not_covered"),
    ("coastal_uncertain", False, "not_covered"),
    ("sea", True, "covered"),
    ("unknown", None, "unknown"),
])
def test_rid_three_kilometre_probe_is_surface_dependent(surface_class, expected_covered, expected_status):
    """同一 provider、同样约 3000 m：land/coastal 与 sea 结论必须不同。"""

    point = [longitude_offset(3000.0), 0.0]
    providers = [rid_provider()]
    probe = sample(longitude=point[0], latitude=point[1], surface_class=surface_class)
    result = evaluate_geometry_point(probe, providers)
    assert result["coverage_status"] == expected_status
    assert result["covered"] is expected_covered
    assert result["surface_class"] == surface_class
    if expected_status == "not_covered":
        assert abs(result["nearest_slant_distance_m"] - 3000.0) < 5.0


def test_rid_land_two_kilometre_boundary_and_sea_five_kilometre_boundary():
    inside_land = sample(longitude=longitude_offset(1990.0), surface_class="land")
    outside_land = sample(longitude=longitude_offset(2010.0), surface_class="land")
    inside_sea = sample(longitude=longitude_offset(4990.0), surface_class="sea")
    outside_sea = sample(longitude=longitude_offset(5010.0), surface_class="sea")
    providers = [rid_provider()]
    assert evaluate_geometry_point(inside_land, providers)["covered"] is True
    assert evaluate_geometry_point(outside_land, providers)["covered"] is False
    assert evaluate_geometry_point(inside_sea, providers)["covered"] is True
    assert evaluate_geometry_point(outside_sea, providers)["covered"] is False


def test_rid_unknown_surface_is_fail_closed_and_never_false_positive():
    """unknown 绝不 false-positive，也绝不回落到 land/sea 半径。"""

    probe = sample(longitude=longitude_offset(100.0), surface_class="unknown")
    result = evaluate_geometry_point(probe, [rid_provider()])
    assert result["covered"] is None
    assert result["coverage_status"] == "unknown"
    assert result["providers"] == []
    assert result["coverage_reason"] == "surface_class_or_radius_unresolved_fail_closed"


def test_rid_effective_radius_resolution_and_urban_fact_only():
    geometry = geometry_block(RID_RADIUS_BY_SURFACE)
    assert effective_radius_m(geometry, "land")["radius_m"] == 2000.0
    assert effective_radius_m(geometry, "coastal_uncertain")["radius_m"] == 2000.0
    assert effective_radius_m(geometry, "sea")["radius_m"] == 5000.0
    assert effective_radius_m(geometry, "unknown")["radius_m"] is None
    assert index_max_range_m(geometry) == 5000.0
    # urban_radius_m 只是设备事实，本轮 urban 分类 disabled，不参与任何半径解析。
    assert RID_URBAN_RADIUS_M == 1000.0 and RID_URBAN_ENABLED is False


# ---------------------------------------------------------------------------
# B. Communication 范围
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface_class", ("land", "coastal_uncertain", "sea"))
def test_communication_four_kilometre_radius_on_every_decidable_surface(surface_class):
    providers = [communication_provider()]
    inside = sample(longitude=longitude_offset(3990.0), surface_class=surface_class)
    outside = sample(longitude=longitude_offset(4010.0), surface_class=surface_class)
    assert evaluate_geometry_point(inside, providers)["covered"] is True
    assert evaluate_geometry_point(outside, providers)["covered"] is False


def test_communication_unknown_surface_is_unknown_not_false():
    probe = sample(longitude=longitude_offset(100.0), surface_class="unknown")
    result = evaluate_geometry_point(probe, [communication_provider()])
    assert result["coverage_status"] == "unknown" and result["covered"] is None


def test_legacy_slant_range_device_keeps_legacy_behaviour_on_unknown_surface():
    """旧设备只有 slant_range_m：任何 surface（含 unknown）都保持旧行为。"""

    legacy = provider(service_key="C:communication", legacy_radius=1000.0, surface_dependent=False)
    probe = sample(longitude=longitude_offset(900.0), surface_class="unknown")
    result = evaluate_geometry_point(probe, [legacy])
    assert result["covered"] is True and result["coverage_status"] == "covered"
    far = sample(longitude=longitude_offset(1100.0), surface_class="unknown")
    assert evaluate_geometry_point(far, [legacy])["covered"] is False


# ---------------------------------------------------------------------------
# C. list 与 GeometricProviderIndex 逐项一致
# ---------------------------------------------------------------------------


def _comparable(result):
    return {
        "covered": result["covered"], "coverage_status": result["coverage_status"],
        "providers": sorted(
            (item["device_id"], round(item["slant_distance_m"], 6),
             item.get("coverage_radius_m"), item.get("effective_surface_class"))
            for item in result["providers"]
        ),
        "nearest_slant_distance_m": (
            None if result["nearest_slant_distance_m"] is None
            else round(result["nearest_slant_distance_m"], 6)
        ),
        "surface_class": result["surface_class"],
    }


@pytest.mark.parametrize("surface_class", SURFACE_CLASSES)
@pytest.mark.parametrize("distance_m", (1999.0, 2001.0, 3999.0, 4001.0, 4999.0, 5001.0, 12000.0))
def test_list_and_index_are_identical_under_surface_dependent_envelopes(surface_class, distance_m):
    """5 km 包络剪枝不得改变任何结论（含 unknown 与边界距离）。"""

    providers = [
        rid_provider(device_id="RID-1", coordinate=(0.0, 0.0), site="site:SITE-A"),
        communication_provider(device_id="COM-1", coordinate=(longitude_offset(200.0), 0.0), site="site:SITE-B"),
        provider(service_key="S:surveillance", device_id="LEGACY-1", legacy_radius=8000.0,
                 coordinate=(longitude_offset(500.0), 0.0), site="site:SITE-C", surface_dependent=False),
        provider(service_key="N:navigation", device_id="NAV-1", legacy_radius=2500.0,
                 coordinate=(longitude_offset(1500.0), 0.0), site=None, surface_dependent=False),
    ]
    probe = sample(longitude=longitude_offset(distance_m), surface_class=surface_class)
    listed = evaluate_geometry_point(probe, providers)
    indexed = GeometricProviderIndex(providers).evaluate(probe)
    assert _comparable(listed) == _comparable(indexed)
    if surface_class == "unknown":
        assert listed["coverage_status"] in ("covered", "unknown", "not_covered")


def test_index_prunes_but_never_changes_a_far_probe_result():
    providers = [rid_provider(coordinate=(0.0, 0.0))]
    far = sample(longitude=longitude_offset(30000.0), surface_class="land")
    indexed = GeometricProviderIndex(providers).evaluate(far)
    assert indexed["covered"] is False and indexed["coverage_status"] == "not_covered"


# ---------------------------------------------------------------------------
# D/E. distinct-site 计数
# ---------------------------------------------------------------------------


def test_communication_distinct_site_counting_land_and_sea():
    # SITE-A 上两台 Communication（同站两设备）：count = 1 < land 要求 2。
    same_site = [
        provider_evaluation(service_key=SERVICE_KEY_COMMUNICATION, site="site:SITE-A", device_id="D1"),
        provider_evaluation(service_key=SERVICE_KEY_COMMUNICATION, site="site:SITE-A", device_id="D2"),
    ]
    land = service_entry(
        summarize_service_redundancy(same_site, REQUIRED_BY_SURFACE, surface_class="land"),
        SERVICE_KEY_COMMUNICATION,
    )
    assert land["distinct_site_count"] == 1
    assert land["required_distinct_site_count"] == 2
    assert land["status"] == "confirmed_deficit"
    assert land["counting_basis"] == "distinct_site_id"

    # SITE-A + SITE-B：count = 2，满足 land。
    two_sites = same_site + [
        provider_evaluation(service_key=SERVICE_KEY_COMMUNICATION, site="site:SITE-B", device_id="D3"),
    ]
    land_ok = service_entry(
        summarize_service_redundancy(two_sites, REQUIRED_BY_SURFACE, surface_class="land"),
        SERVICE_KEY_COMMUNICATION,
    )
    assert land_ok["distinct_site_count"] == 2 and land_ok["status"] == "satisfied"

    # 海上单站即可满足。
    sea_ok = service_entry(
        summarize_service_redundancy(
            same_site, REQUIRED_BY_SURFACE, surface_class="sea"),
        SERVICE_KEY_COMMUNICATION,
    )
    assert sea_ok["required_distinct_site_count"] == 1 and sea_ok["status"] == "satisfied"

    # coastal_uncertain 始终按 land（要求 2）。
    coastal = service_entry(
        summarize_service_redundancy(same_site, REQUIRED_BY_SURFACE, surface_class="coastal_uncertain"),
        SERVICE_KEY_COMMUNICATION,
    )
    assert coastal["required_distinct_site_count"] == 2 and coastal["status"] == "confirmed_deficit"


def test_rid_distinct_site_counting_land_and_sea():
    same_site = [
        provider_evaluation(service_key=SERVICE_KEY_RID_COOPERATIVE, site="site:SITE-A", device_id="D1"),
        provider_evaluation(service_key=SERVICE_KEY_RID_COOPERATIVE, site="site:SITE-A", device_id="D2"),
    ]
    land = service_entry(
        summarize_service_redundancy(same_site, REQUIRED_BY_SURFACE, surface_class="land"),
        SERVICE_KEY_RID_COOPERATIVE,
    )
    assert land["distinct_site_count"] == 1 and land["status"] == "confirmed_deficit"

    two_sites = same_site + [
        provider_evaluation(service_key=SERVICE_KEY_RID_COOPERATIVE, site="site:SITE-B", device_id="D3"),
    ]
    assert service_entry(
        summarize_service_redundancy(two_sites, REQUIRED_BY_SURFACE, surface_class="land"),
        SERVICE_KEY_RID_COOPERATIVE,
    )["status"] == "satisfied"

    sea = service_entry(
        summarize_service_redundancy(same_site, REQUIRED_BY_SURFACE, surface_class="sea"),
        SERVICE_KEY_RID_COOPERATIVE,
    )
    assert sea["required_distinct_site_count"] == 1 and sea["status"] == "satisfied"


# ---------------------------------------------------------------------------
# F. service 隔离
# ---------------------------------------------------------------------------


def test_communication_and_rid_never_share_a_redundancy_pool():
    evaluations = [
        provider_evaluation(service_key=SERVICE_KEY_COMMUNICATION, site="site:SITE-A", device_id="C1"),
        provider_evaluation(service_key=SERVICE_KEY_RID_COOPERATIVE, site="site:SITE-B", device_id="R1"),
    ]
    pools = summarize_service_redundancy(evaluations, REQUIRED_BY_SURFACE, surface_class="land")
    communication = service_entry(pools, SERVICE_KEY_COMMUNICATION)
    rid = service_entry(pools, SERVICE_KEY_RID_COOPERATIVE)
    assert communication["distinct_site_count"] == 1
    assert rid["distinct_site_count"] == 1
    # 绝不能满足任何一个 service 的 2 站要求。
    assert communication["status"] == "confirmed_deficit"
    assert rid["status"] == "confirmed_deficit"


def test_same_physical_site_carries_both_services_without_mixing():
    evaluations = [
        provider_evaluation(service_key=SERVICE_KEY_COMMUNICATION, site="tower:TOWER-1", device_id="C1"),
        provider_evaluation(service_key=SERVICE_KEY_RID_COOPERATIVE, site="tower:TOWER-1", device_id="R1"),
    ]
    pools = summarize_service_redundancy(evaluations, REQUIRED_BY_SURFACE, surface_class="sea")
    communication = service_entry(pools, SERVICE_KEY_COMMUNICATION)
    rid = service_entry(pools, SERVICE_KEY_RID_COOPERATIVE)
    assert communication["distinct_site_count"] == 1 and rid["distinct_site_count"] == 1
    assert communication["status"] == "satisfied" and rid["status"] == "satisfied"


# ---------------------------------------------------------------------------
# H. 无物理身份 fail-closed
# ---------------------------------------------------------------------------


def test_unknown_physical_identity_is_fail_closed_when_more_than_one_site_is_required():
    evaluations = [
        provider_evaluation(service_key=SERVICE_KEY_COMMUNICATION, site=None, device_id="D1"),
        provider_evaluation(service_key=SERVICE_KEY_COMMUNICATION, site=None, device_id="D2"),
    ]
    entry = service_entry(
        summarize_service_redundancy(evaluations, REQUIRED_BY_SURFACE, surface_class="land"),
        SERVICE_KEY_COMMUNICATION,
    )
    assert entry["distinct_site_count"] is None
    assert entry["status"] == "unknown"
    # 两台设备的 device_id 绝不能被当成两个站址。
    assert distinct_site_provider_count(evaluations) is None


def test_unknown_surface_makes_required_count_unknown_even_with_two_sites():
    evaluations = [
        provider_evaluation(service_key=SERVICE_KEY_COMMUNICATION, site="site:SITE-A", device_id="D1"),
        provider_evaluation(service_key=SERVICE_KEY_COMMUNICATION, site="site:SITE-B", device_id="D2"),
    ]
    entry = service_entry(
        summarize_service_redundancy(evaluations, REQUIRED_BY_SURFACE, surface_class="unknown"),
        SERVICE_KEY_COMMUNICATION,
    )
    assert entry["required_distinct_site_count"] is None
    assert entry["status"] == "unknown"


# ---------------------------------------------------------------------------
# distinct_site_id 解析顺序与 service_key 解析
# ---------------------------------------------------------------------------


def test_distinct_site_id_resolution_order_never_guesses_coordinates():
    tower = {"facility_id": "F1", "site_id": "SITE-A", "coordinate": [122.0, 30.0],
             "metadata": {"host": {"host_type": "tower", "host_tower_id": "T1"}}}
    assert distinct_site_id_for(tower) == "tower:T1"
    existing = {"facility_id": "F2", "site_id": "SITE-B", "coordinate": [122.1, 30.1]}
    assert distinct_site_id_for(existing) == "site:SITE-B"
    facility_only = {"facility_id": "F3", "coordinate": [122.2, 30.2]}
    assert distinct_site_id_for(facility_only) == "facility:F3"
    # 无法得到可靠物理身份 ⇒ None（绝不用坐标取整兜底）。
    assert distinct_site_id_for({"coordinate": [122.3, 30.3]}) is None
    assert distinct_site_id_for({"device_id": "D9"}) is None


def test_service_key_resolution_prefers_explicit_then_rid_type_then_legacy():
    assert service_key_for("C", {"service_key": "C:communication"}) == SERVICE_KEY_COMMUNICATION
    assert service_key_for("S", {"service_key": "S:rid_cooperative"}) == SERVICE_KEY_RID_COOPERATIVE
    rid = {"subsystem": "S", "type": {"service_subtype": "cooperative_surveillance",
                                     "technology": "network_remote_id",
                                     "target_cooperation": "cooperative"}}
    assert service_key_for("S", rid) == SERVICE_KEY_RID_COOPERATIVE
    assert service_contract_for("S", rid)["service_surface_dependent"] is True
    # 旧项目：没有 service_key / 没有 RID 标识 ⇒ legacy key 且 surface policy 不生效。
    legacy = service_contract_for("S", {"subsystem": "S", "type": {"technology": "radar"}})
    assert legacy["service_key"] == "S:surveillance"
    assert legacy["service_surface_dependent"] is False
    # 显式声明与 subsystem 冲突时回落 legacy，绝不静默改派。
    conflicting = service_contract_for("S", {"service_key": "C:communication"})
    assert conflicting["service_key"] == "S:surveillance"
    assert conflicting["service_surface_dependent"] is False


def test_not_evaluated_declarations_are_explicit():
    assert set(COMMUNICATION_NOT_EVALUATED) >= {
        "terrain_LOS", "building_blocking", "diffraction", "interference",
        "link_budget", "capacity", "throughput", "latency",
    }
    assert set(RID_NOT_EVALUATED) >= {
        "terrain_LOS", "building_blocking", "RF_propagation", "receiver_sensitivity",
        "packet_collision", "interference", "real_antenna_pattern",
    }
    pools = summarize_service_redundancy(
        [provider_evaluation(service_key=SERVICE_KEY_RID_COOPERATIVE, site="site:SITE-A")],
        REQUIRED_BY_SURFACE, surface_class="sea",
    )
    assert set(service_entry(pools, SERVICE_KEY_RID_COOPERATIVE)["not_evaluated"]) >= set(RID_NOT_EVALUATED)


# ---------------------------------------------------------------------------
# 附加：P7 → P8 端到端（真实 build_geometric_providers 调用链）
# ---------------------------------------------------------------------------


def rid_device(device_id, *, site_id=None, radius_by_surface=None):
    return normalize_device({
        "device_id": device_id, "name": device_id, "subsystem": "S", "role": "existing",
        "radius_m": 2000.0, "mtbf_h": 1000,
        "service_key": SERVICE_KEY_RID_COOPERATIVE,
        "type": {
            "service_subtype": "cooperative_surveillance", "technology": "network_remote_id",
            "target_cooperation": "cooperative",
        },
        "performance": {"min_redundancy": 1},
        "coverage_geometry": {
            "model": "hemisphere", "source": "test", "confirmed": True,
            "radius_by_surface": dict(radius_by_surface or RID_RADIUS_BY_SURFACE),
            "urban_radius_m": RID_URBAN_RADIUS_M, "urban_enabled": False,
        },
        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "network_remote_id", "parameters": {"performance": {}},
            "source": "test", "confirmed": True,
        },
    })


def communication_device(device_id):
    return normalize_device({
        "device_id": device_id, "name": device_id, "subsystem": "C", "role": "existing",
        "radius_m": 4000.0, "mtbf_h": 1000,
        "service_key": SERVICE_KEY_COMMUNICATION,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 1.0, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "hemisphere", "source": "test", "confirmed": True,
            "radius_by_surface": dict(COMMUNICATION_RADIUS_BY_SURFACE),
        },
        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "dedicated_radio",
            "parameters": {"performance": {"max_latency_s": 0.5}},
            "source": "test", "confirmed": True,
        },
    })


def facilities(items):
    return {"status": "passed", "items": items}


def test_build_geometric_providers_carries_service_key_and_distinct_site_id():
    collection = facilities([
        {"facility_id": "F1", "site_id": "SITE-A", "coordinate": [0.0, 0.0], "status": "active",
         "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
         "devices": [{"device_id": "RID-1", "subsystem": "S", "status": "active"}]},
        {"facility_id": "F2", "site_id": "SITE-B", "coordinate": [longitude_offset(3000.0), 0.0],
         "status": "active",
         "metadata": {"host": {"host_type": "tower", "host_tower_id": "T-B"}},
         "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
         "devices": [{"device_id": "RID-2", "subsystem": "S", "status": "active"}]},
    ])
    catalog = {"status": "passed", "items": [rid_device("RID-1"), rid_device("RID-2")]}
    providers = build_geometric_providers(collection, catalog)["S"]
    assert [item["service_key"] for item in providers] == [SERVICE_KEY_RID_COOPERATIVE] * 2
    assert all(item["service_surface_dependent"] is True for item in providers)
    assert sorted(item["distinct_site_id"] for item in providers) == ["site:SITE-A", "tower:T-B"]


def test_p8_service_redundancy_is_computed_per_sample_surface():
    collection = facilities([
        {"facility_id": "F1", "site_id": "SITE-A", "coordinate": [0.0, 0.0], "status": "active",
         "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
         "devices": [{"device_id": "RID-1", "subsystem": "S", "status": "active"}]},
        {"facility_id": "F2", "site_id": "SITE-B", "coordinate": [longitude_offset(1000.0), 0.0],
         "status": "active",
         "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
         "devices": [{"device_id": "RID-2", "subsystem": "S", "status": "active"}]},
    ])
    catalog = {"status": "passed", "items": [rid_device("RID-1"), rid_device("RID-2")]}
    providers = build_geometric_providers(collection, catalog)["S"]
    requirement = {
        "required": True, "status": "passed",
        "type": {"technology": "network_remote_id", "target_cooperation": "cooperative"},
        "performance": {"min_redundancy": 1},
        "redundancy_by_surface": {"land": 2, "coastal_uncertain": 2, "sea": 1},
    }
    aircraft = {"surveillance": {"confirmed": True, "status": "confirmed", "capabilities": ["rid"],
                                 "type": {"technology": "network_remote_id",
                                          "target_cooperation": "cooperative"},
                                 "performance": {}}}
    devices = {item["device_id"]: item for item in catalog["items"]}

    land_probe = sample(longitude=longitude_offset(500.0), surface_class="land")
    land_geometry = evaluate_geometry_point(land_probe, providers)
    land_capability = evaluate_capability_point(
        "S", {**land_probe, **land_geometry}, requirement, aircraft, devices,
    )
    land_entry = service_entry(land_capability["service_redundancy"], SERVICE_KEY_RID_COOPERATIVE)
    assert land_entry["distinct_site_count"] == 2
    assert land_entry["required_distinct_site_count"] == 2
    assert land_entry["status"] == "satisfied"
    assert land_entry["surface_class"] == "land"

    # 同一批 provider、同一点，改成海面 ⇒ 要求降为 1 且保持 satisfied（不是靠固定 2/1）。
    sea_probe = {**land_probe, "surface_class": "sea"}
    sea_geometry = evaluate_geometry_point(sea_probe, providers)
    sea_capability = evaluate_capability_point(
        "S", {**sea_probe, **sea_geometry}, requirement, aircraft, devices,
    )
    sea_entry = service_entry(sea_capability["service_redundancy"], SERVICE_KEY_RID_COOPERATIVE)
    assert sea_entry["required_distinct_site_count"] == 1 and sea_entry["status"] == "satisfied"

    # unknown surface ⇒ P7 覆盖状态未知 ⇒ P8 保持 unknown，且**不** invent 任何
    # service 级结论（service_redundancy 为空，fail-closed）。
    unknown_probe = {**land_probe, "surface_class": "unknown"}
    unknown_geometry = evaluate_geometry_point(unknown_probe, providers)
    assert unknown_geometry["covered"] is None and unknown_geometry["coverage_status"] == "unknown"
    unknown_capability = evaluate_capability_point(
        "S", {**unknown_probe, **unknown_geometry}, requirement, aircraft, devices,
    )
    assert unknown_capability["status"] == "unknown"
    assert unknown_capability["surface_class"] == "unknown"
    assert not unknown_capability.get("service_redundancy")


# ---------------------------------------------------------------------------
# 附加：P15 corridor gap 按 voxel surface 解析要求站址数
# ---------------------------------------------------------------------------


def voxel_subsystem(evaluations, *, surface_class="land", p8_status="meets_under_model"):
    return {
        "subsystem": "S", "planning_status": "satisfied", "p8_status": p8_status,
        "provider_evaluations": evaluations, "surface_class": surface_class,
        "reasons": [], "evidence": [],
    }


def corridor_input(land_evaluations, *, surface_class="land"):
    return {
        "status": "passed", "algorithm_id": "cns_service_corridor_v1", "algorithm_version": "1.0",
        "input_fingerprint": "test", "route_count": 1,
        "routes": [{
            "route_id": "R1", "route_length_m": 1000.0, "status": "passed",
            "voxels": [{
                "voxel_id": "V1", "grid_id": "G1", "altitude_layer_id": "L1",
                "nearest_route_offset_m": 500.0, "cell_half_diagonal_m": 100.0,
                "discretized_volume_proxy_m3": 1000.0, "surface_class": surface_class,
                "subsystems": [voxel_subsystem(land_evaluations, surface_class=surface_class)],
            }],
            "subsystems": [],
        }],
    }


def subsystem_entry(voxel, code):
    matches = [item for item in voxel["subsystems"] if item["subsystem"] == code]
    assert len(matches) == 1, f"expected exactly one {code} entry, got {matches}"
    return matches[0]


def test_p15_gap_resolves_required_site_count_from_voxel_surface():
    requirement = {"status": "passed", "project_default": {
        "communication": {"required": False}, "navigation": {"required": False},
        "surveillance": {
            "required": True, "status": "passed", "type": {}, "performance": {"min_redundancy": 1},
            "redundancy_by_surface": {"land": 2, "coastal_uncertain": 2, "sea": 1},
        },
    }, "route_overrides": {}}
    one_site = [provider_evaluation(service_key=SERVICE_KEY_RID_COOPERATIVE, site="site:SITE-A", device_id="R1")]
    two_sites = one_site + [
        provider_evaluation(service_key=SERVICE_KEY_RID_COOPERATIVE, site="site:SITE-B", device_id="R2"),
    ]

    land_single = CNSCorridorGapAnalyzerV1().evaluate(
        corridor_input(one_site, surface_class="land"), requirement, {})
    land_voxel = subsystem_entry(land_single["routes"][0]["voxels"][0], "S")
    assert land_voxel["services"][0]["required_distinct_site_count"] == 2
    assert land_voxel["services"][0]["distinct_site_count"] == 1
    assert land_voxel["combined_status"] == "confirmed_gap"
    assert "redundancy_deficit" in land_voxel["causes"]

    land_two = CNSCorridorGapAnalyzerV1().evaluate(
        corridor_input(two_sites, surface_class="land"), requirement, {})
    assert subsystem_entry(land_two["routes"][0]["voxels"][0], "S")["combined_status"] == "satisfied"

    # 同一份单站证据在海上（要求 1）必须 satisfied —— 全航路固定 redundancy=2 是错的。
    sea_single = CNSCorridorGapAnalyzerV1().evaluate(
        corridor_input(one_site, surface_class="sea"), requirement, {})
    sea_voxel = subsystem_entry(sea_single["routes"][0]["voxels"][0], "S")
    assert sea_voxel["services"][0]["required_distinct_site_count"] == 1
    assert sea_voxel["combined_status"] == "satisfied"

    # unknown surface ⇒ 要求未知 ⇒ 不得 satisfied。
    unknown_single = CNSCorridorGapAnalyzerV1().evaluate(
        corridor_input(one_site, surface_class="unknown"), requirement, {})
    unknown_voxel = subsystem_entry(unknown_single["routes"][0]["voxels"][0], "S")
    assert unknown_voxel["services"][0]["required_distinct_site_count"] is None
    assert unknown_voxel["combined_status"] == "unknown"


# ---------------------------------------------------------------------------
# 附加：normalize 层的 additive 形状与兼容性
# ---------------------------------------------------------------------------


def test_normalize_coverage_geometry_is_additive_and_keeps_legacy_shape():
    legacy = normalize_coverage_geometry({}, {"model": "sphere", "slant_range_m": 700.0,
                                              "source": "test", "confirmed": True})
    assert legacy == {
        "model": "sphere", "slant_range_m": 700.0, "model_scope": "geometric_only",
        "source": "test", "confirmed": True, "status": "confirmed",
    }
    extended = normalize_coverage_geometry({}, {
        "model": "hemisphere", "source": "test", "confirmed": True,
        "radius_by_surface": {"land": 2000, "coastal_uncertain": 2000, "sea": 5000},
        "urban_radius_m": 1000, "urban_enabled": False,
    })
    assert extended["slant_range_m"] is None
    assert extended["status"] == "confirmed"
    assert extended["radius_by_surface"] == {"land": 2000.0, "coastal_uncertain": 2000.0, "sea": 5000.0}
    assert extended["urban_radius_m"] == 1000.0 and extended["urban_enabled"] is False
    with pytest.raises(ValueError):
        normalize_coverage_geometry({}, {"model": "hemisphere", "confirmed": True, "radius_by_surface": {}})
    with pytest.raises(ValueError):
        normalize_coverage_geometry({}, {"model": "hemisphere", "confirmed": True,
                                         "radius_by_surface": {"land": -1}})


def test_normalize_device_keeps_legacy_shape_when_no_service_identity():
    base = {
        "device_id": "D1", "name": "D1", "subsystem": "C", "role": "existing",
        "radius_m": 100.0, "mtbf_h": 1000, "type": {"technology": "dedicated_radio"},
        "performance": {"min_redundancy": 1},
        "coverage_geometry": {"model": "sphere", "slant_range_m": 100.0, "confirmed": True},
    }
    plain = normalize_device(dict(base))
    assert "service_key" not in plain and "service_subtype" not in plain
    declared = normalize_device({**base, "service_key": "C:communication"})
    assert declared["service_key"] == "C:communication"
    assert distance_m([0.0, 0.0], [0.0, 0.0]) == 0.0


def test_required_distinct_site_count_prefers_explicit_requirement_over_policy():
    explicit = {"redundancy_by_surface": {"land": 3, "coastal_uncertain": 3, "sea": 1}}
    assert required_distinct_site_count(explicit, SERVICE_KEY_COMMUNICATION, "land") == 3
    assert required_distinct_site_count(explicit, SERVICE_KEY_COMMUNICATION, "unknown") is None
    # 未声明 redundancy_by_surface 时用该 service 的冻结 policy。
    assert required_distinct_site_count({}, SERVICE_KEY_RID_COOPERATIVE, "land") == 2
    assert required_distinct_site_count({}, SERVICE_KEY_RID_COOPERATIVE, "sea") == 1
    assert required_distinct_site_count({}, SERVICE_KEY_RID_COOPERATIVE, "unknown") is None
    # legacy（无 surface policy）：回到 min_redundancy，与 surface 无关。
    legacy_required = {"performance": {"min_redundancy": 4}}
    assert required_distinct_site_count(legacy_required, "S:surveillance", "unknown") == 4
    assert required_distinct_site_count(legacy_required, "S:surveillance", "land") == 4
