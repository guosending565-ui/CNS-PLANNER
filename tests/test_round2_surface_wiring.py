"""Round 2 P0：Application 正式接线 + Round 1 语义收口。

覆盖本轮 16 项验收：

1.  ADS-B + cooperative != RID；``network_remote_id`` = RID
2.  RequiredCNS ``service_key`` / ``service_subtype`` / ``redundancy_by_surface`` round-trip
3.  非法 ``service_key`` fail-closed
4.  Existing CNS 内联 RID device normalize -> save/reopen 后仍为 ``S:rid_cooperative``
5.  ``radius_by_surface`` device 可通过 CandidateAction geometry gate
6.  TowerColocation ``distinct_site_id`` 稳定（``tower:<host_tower_id>``）
7.  真实 land-mask surface facts：land / coastal_uncertain / sea / unknown
8.  surface fact source/policy 改变 => Coverage/Corridor fingerprint 改变且下游 stale
9.  Heavy task immutable snapshot 不含 callable/QGIS object，save/load 后 surface facts 相同
10. RID：3 km land 不覆盖；3 km sea 覆盖
11. Communication：land 单站 = 覆盖有 / 冗余不足；land 两站 = satisfied；sea 单站 = satisfied
12. RID：land 单站 = 冗余不足；land 两站 = satisfied；sea 单站 = satisfied
13. 同塔双设备不能算双站
14. Communication / RID 不混池
15. TowerColocation 能实际选择 surface-aware device（不被 ``slant_range_m=None`` 阻塞）
16. 旧 C/N/S fixture 行为保持兼容
"""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.coverage.geometric_3d import evaluate_geometry_point
from cns_planner.application.corridor_service import CNSCorridorService
from cns_planner.application.site_candidate_actions import candidate_actions
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_corridor import normalize_cns_corridor_policy
from cns_planner.domain.cns_inputs import (
    normalize_aircraft_profile, normalize_candidate_site, normalize_device,
    normalize_existing_facility, normalize_required_cns,
)
from cns_planner.domain.cns_service_contract import (
    COMMUNICATION_RADIUS_BY_SURFACE, RID_RADIUS_BY_SURFACE,
    SERVICE_KEY_COMMUNICATION, SERVICE_KEY_RID_COOPERATIVE,
    distinct_site_id_for, effective_radius_m, is_rid_declaration,
    normalize_redundancy_by_surface, service_contract_for, service_key_for,
    validated_service_key,
)
from cns_planner.domain.surface_classification import (
    POLICY_ORIGIN_LEGACY_RADAR_MIGRATION, SurfaceFactsProvider,
    build_surface_class_facts, empty_surface_class_facts,
    normalize_surface_class_facts, surface_facts_input_fingerprint,
)
from cns_planner.domain.tower_colocation import (
    build_tower_colocation_candidates, normalize_tower_colocation_policy,
)
from cns_planner.gis.radar_layout_adapter import LandMaskSource
from cns_planner.persistence.artifact_store import (
    deserialize_payload, serialize_payload,
)


DEFAULTS = Path("cns_planner/config/defaults.json")
EARTH_RADIUS_M = 6_371_008.8


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def longitude_offset(metres, latitude=0.0):
    import math

    return math.degrees(metres / (EARTH_RADIUS_M * math.cos(math.radians(latitude))))


def vertical(origin=100.0):
    return {
        "surface_elevation_m": 0.0, "surface_vertical_reference": "egm2008_orthometric",
        "mount_height_agl_m": origin, "service_origin_egm2008_m": origin,
        "source": "test", "confirmed": True, "status": "confirmed",
    }


def rid_device(identifier="RID1", *, radii=None, urban=None):
    block = {
        "model": "hemisphere", "slant_range_m": None, "model_scope": "geometric_only",
        "source": "test", "confirmed": True, "status": "confirmed",
        "radius_by_surface": dict(radii or RID_RADIUS_BY_SURFACE),
    }
    if urban is not None:
        block["urban_radius_m"] = urban
        block["urban_enabled"] = False
    return normalize_device({
        "device_id": identifier, "name": identifier, "subsystem": "S", "role": "existing",
        "radius_m": 2000.0, "mtbf_h": 1000,
        "type": {
            "technology": "network_remote_id", "target_cooperation": "cooperative",
            "service_subtype": "cooperative_surveillance", "sensor_mode": "passive",
        },
        "service_key": SERVICE_KEY_RID_COOPERATIVE,
        "performance": {"min_redundancy": 1},
        "coverage_geometry": block,        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "network_remote_id",
            #: 已声明性能必须与 RequiredCNS 的可判定项一致：P8 逐项核对，
            #: 缺失即 "unknown"（证据不足），不会静默判满足。
            "parameters": {"performance": {"max_update_interval_s": 5.0},
                           "independence_confirmed": True,
                           "independence_group": identifier},
            "source": "test", "confirmed": True,
        },
    })


def communication_device(identifier="COM1", *, radii=None):
    return normalize_device({
        "device_id": identifier, "name": identifier, "subsystem": "C", "role": "existing",
        "radius_m": 4000.0, "mtbf_h": 1000,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "service_key": SERVICE_KEY_COMMUNICATION,
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "hemisphere", "slant_range_m": None, "model_scope": "geometric_only",
            "source": "test", "confirmed": True, "status": "confirmed",
            "radius_by_surface": dict(radii or COMMUNICATION_RADIUS_BY_SURFACE),
        },
        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "dedicated_radio",
            "parameters": {"performance": {"max_latency_s": 0.2},
                           "independence_confirmed": True, "independence_group": identifier},
            "source": "test", "confirmed": True,
        },
    })


def legacy_device(identifier="LEG1", subsystem="C"):
    return normalize_device({
        "device_id": identifier, "name": identifier, "subsystem": subsystem,
        "role": "existing", "radius_m": 3000.0, "mtbf_h": 1000,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "hemisphere", "slant_range_m": 3000.0,
            "source": "test", "confirmed": True,
        },
        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "dedicated_radio",
            "parameters": {"performance": {"max_latency_s": 0.2},
                           "independence_confirmed": True, "independence_group": identifier},
            "source": "test", "confirmed": True,
        },
    })


def facility(identifier, coordinate, devices, *, site_id=None):
    return normalize_existing_facility({
        "facility_id": identifier, "site_id": site_id or identifier, "name": identifier,
        "coordinate": list(coordinate), "vertical_profile": vertical(),
        "devices": devices, "status": "active", "source": "test",
        "planning_profile": {
            "reuse_class": "existing_shared_site", "add_device_allowed": True,
            "source": "test", "confirmed": True,
        },
    })


def land_mask_geojson(tmp_path, *, name="land.geojson", offset=(0.0, 0.0), size=0.002):
    """只读陆域矩形：相对 ``offset`` 的 ``size`` x ``size`` 方块（度）。"""

    west, south = offset
    east, north = west + size, south + size
    payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [west, south], [east, south], [east, north], [west, north], [west, south],
                ]],
            },
            "properties": {"name": "land"},
        }],
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def grid_cells(mapping):
    """``{grid_id: (west, south, east, north)}`` -> grid 容器。"""

    return {
        "status": "passed", "crs": "OGC:CRS84", "level": 8,
        "cells": [
            {"grid_id": grid_id, "bbox": list(bbox)} for grid_id, bbox in mapping.items()
        ],
    }


def route_grid(*, latitude=0.0003, height=0.0004, step=0.0007, count=31):
    """沿航路的**小格**网格：格心逐个落在真实 land / sea 区域内。

    格心代表分类只有在格子足够小时才与真实陆海分布一致；大格跨越海岸线会让整个格
    （含内陆部分）都取到海中代表点。这不是分类错误，而是"格心代表"语义的直接后果，
    因此测试使用小格，使 land / sea 结论真实可验证。
    """

    cells = []
    for index in range(count):
        west = round(index * step, 6)
        cells.append({
            "grid_id": f"G{index + 1:02d}",
            "bbox": [west, latitude, round(west + step, 6), latitude + height],
        })
    return {"status": "passed", "crs": "OGC:CRS84", "level": 8, "cells": cells}


def provider_for(grid, land_mask_path, *, buffer_m=30.0):
    if land_mask_path is None:
        facts = build_surface_class_facts(
            grid, policy={"coastal_uncertainty_buffer_m": buffer_m, "confirmed": True},
            land_mask_source=None,
        )
        return facts, SurfaceFactsProvider(facts, grid=grid)
    source = LandMaskSource(land_mask_path, coastal_uncertainty_buffer_m=buffer_m)
    facts = build_surface_class_facts(
        grid, policy={"coastal_uncertainty_buffer_m": buffer_m, "confirmed": True},
        land_mask_source=source, land_mask_describe=source.describe,
    )
    return facts, SurfaceFactsProvider(facts, grid=grid)


def requirement_set(*, service="surveillance", redundancy_by_surface=None, service_key=None):
    """与 Round 1 同一口径的最小可用 RequiredCNS（surface 冗余由 redundancy_by_surface 承载）。"""

    values = {
        "communication": {"required": False, "status": "passed", "type": {}, "performance": {}},
        "navigation": {"required": False, "status": "passed", "type": {}, "performance": {}},
        "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
    }
    entry = {
        "required": True, "status": "passed",
        "type": {}, "performance": {"min_redundancy": 1},
    }
    if service == "surveillance":
        entry["type"] = {"technology": "network_remote_id", "target_cooperation": "cooperative",
                         "service_subtype": "cooperative_surveillance"}
    else:
        entry["type"] = {"technology": "dedicated_radio", "interfaces": ["ip"]}
        entry["performance"] = {"max_latency_s": 0.5, "min_redundancy": 1}
    if redundancy_by_surface is not None:
        entry["redundancy_by_surface"] = dict(redundancy_by_surface)
    if service_key is not None:
        entry["service_key"] = service_key
    values[service] = entry
    return {"status": "passed", "project_default": values, "route_overrides": {}}


# ---------------------------------------------------------------------------
# 1. RID declaration 收口
# ---------------------------------------------------------------------------


def test_adsb_cooperative_is_not_rid():
    """ADS-B 同样是合作监视：单凭 cooperative 不足以认定 RID。"""

    adsb = {
        "subsystem": "S",
        "type": {"technology": "adsb", "target_cooperation": "cooperative",
                 "service_subtype": "cooperative_surveillance"},
    }
    assert is_rid_declaration(adsb) is False
    assert service_key_for("S", adsb) == "S:surveillance"
    assert service_contract_for("S", adsb)["service_surface_dependent"] is False

    # 反面：两个**排他**事实各自都足以认定 RID。
    assert is_rid_declaration({"type": {"technology": "network_remote_id"}}) is True
    assert is_rid_declaration({"service_key": SERVICE_KEY_RID_COOPERATIVE}) is True
    assert service_key_for(
        "S", {"type": {"technology": "network_remote_id"}}
    ) == SERVICE_KEY_RID_COOPERATIVE
    assert service_key_for(
        "S", {"service_key": SERVICE_KEY_RID_COOPERATIVE}
    ) == SERVICE_KEY_RID_COOPERATIVE

    # 泛化 subtype 单独出现（无 technology / service_key）同样不足以认定 RID。
    assert is_rid_declaration(
        {"type": {"service_subtype": "cooperative_surveillance"}}
    ) is False


def test_generic_cooperative_surveillance_does_not_enable_surface_policy():
    generic = {"subsystem": "S", "performance": {"min_redundancy": 2},
               "type": {"service_subtype": "cooperative_surveillance"}}
    contract = service_contract_for("S", generic)
    assert contract["service_key"] == "S:surveillance"
    assert contract["service_surface_dependent"] is False
    assert contract["policy"] is None


# ---------------------------------------------------------------------------
# 2/3. RequiredCNS canonical service identity
# ---------------------------------------------------------------------------


def test_required_cns_round_trips_service_identity_and_redundancy():
    payload = requirement_set(
        service="surveillance",
        redundancy_by_surface={"land": 2, "coastal_uncertain": 2, "sea": 1},
        service_key=SERVICE_KEY_RID_COOPERATIVE,
    )
    normalized = normalize_required_cns(payload)
    entry = normalized["project_default"]["surveillance"]
    assert entry["service_key"] == SERVICE_KEY_RID_COOPERATIVE
    assert entry["service_subtype"] == "cooperative_surveillance"
    assert entry["type"]["technology"] == "network_remote_id"
    assert entry["type"]["target_cooperation"] == "cooperative"
    assert entry["redundancy_by_surface"] == {"land": 2, "coastal_uncertain": 2, "sea": 1}

    # round-trip：normalize 的输出再 normalize 完全一致（save/reopen 不丢字段）。
    assert normalize_required_cns(deepcopy(normalized)) == normalized
    # JSON 往返（等价于磁盘持久化）后同样保留。
    rehydrated = json.loads(json.dumps(normalized, ensure_ascii=False))
    assert normalize_required_cns(rehydrated) == normalized


def test_illegal_service_key_is_fail_closed_not_silently_legacy():
    with pytest.raises(ValueError):
        validated_service_key("C:comunication")
    # 子系统前缀不一致同样拒绝。
    with pytest.raises(ValueError):
        validated_service_key(SERVICE_KEY_RID_COOPERATIVE, subsystem="C")

    bad = {
        "status": "passed",
        "project_default": {"surveillance": {
            "required": True, "status": "passed", "type": {}, "performance": {},
            "service_key": "S:rid_cooperativ",
            "redundancy_by_surface": {"land": 2, "sea": 1},
        }},
        "route_overrides": {},
    }
    with pytest.raises(ValueError):
        normalize_required_cns(bad)

    bad_device = {"device_id": "X", "subsystem": "S", "role": "existing", "radius_m": 1000,
                  "mtbf_h": 1, "service_key": "S:surveilance"}
    with pytest.raises(ValueError):
        normalize_device(bad_device)

    # 未显式声明时保持 legacy（不报错、不启用 surface policy）。
    assert validated_service_key(None) is None
    legacy = normalize_required_cns(None)["project_default"]["communication"]
    assert legacy["status"] == "pending_confirmation"
    assert "service_key" not in legacy


def test_redundancy_by_surface_canonical_validation():
    assert normalize_redundancy_by_surface({"land": 2, "sea": 1}) == {"land": 2, "sea": 1}
    assert normalize_redundancy_by_surface(None) is None
    for bad in (
        {"land": 0}, {"land": -1}, {"land": 1.5}, {"land": True},
        {"land": 2, "unknown": 1}, {"land": 2, "coast": 1}, {"land": "x"},
    ):
        with pytest.raises(ValueError):
            normalize_redundancy_by_surface(bad)
    # 旧项目只有 min_redundancy 时保持 legacy。
    legacy = normalize_required_cns({
        "status": "passed",
        "project_default": {"communication": {
            "required": True, "status": "passed", "type": {},
            "performance": {"min_redundancy": 3},
        }},
        "route_overrides": {},
    })
    assert "redundancy_by_surface" not in legacy["project_default"]["communication"]
    assert legacy["project_default"]["communication"]["performance"]["min_redundancy"] == 3


# ---------------------------------------------------------------------------
# 4. Existing inline RID device 的 save/reopen
# ---------------------------------------------------------------------------


def test_existing_inline_rid_device_survives_normalize_and_reopen(tmp_path):
    inline = {
        "device_id": "INLINE-RID", "name": "inline", "subsystem": "S", "status": "active",
        "service_key": SERVICE_KEY_RID_COOPERATIVE,
        "service_subtype": "cooperative_surveillance",
        "type": {"technology": "network_remote_id", "target_cooperation": "cooperative"},
        "performance": {"min_redundancy": 1},
        "coverage_geometry": {
            "model": "hemisphere", "slant_range_m": None, "source": "test",
            "confirmed": True, "radius_by_surface": dict(RID_RADIUS_BY_SURFACE),
            "urban_radius_m": 1000.0, "urban_enabled": False,
        },
        "planning_origin": {"origin": "test", "host_tower_id": "T-7"},
    }
    normalized = normalize_existing_facility({
        "facility_id": "F1", "site_id": "S1", "coordinate": [0.0005, 0.0005],
        "vertical_profile": vertical(), "devices": [inline],
        "status": "active", "source": "test",
    })
    device = normalized["devices"][0]
    assert device["service_key"] == SERVICE_KEY_RID_COOPERATIVE
    assert device["service_subtype"] == "cooperative_surveillance"
    assert device["type"]["technology"] == "network_remote_id"
    assert device["performance"]["min_redundancy"] == 1
    assert device["coverage_geometry"]["radius_by_surface"] == RID_RADIUS_BY_SURFACE
    assert device["coverage_geometry"]["urban_radius_m"] == 1000.0
    assert device["coverage_geometry"]["urban_enabled"] is False
    assert device["planning_origin"] == {"origin": "test", "host_tower_id": "T-7"}
    # 即使 device_catalog 完全不提供该设备，它依然被识别为 RID。
    assert is_rid_declaration(device) is True
    assert service_key_for(device["subsystem"], device) == SERVICE_KEY_RID_COOPERATIVE

    # normalize 幂等（save/reopen 后形状稳定）。
    assert normalize_existing_facility(deepcopy(normalized)) == normalized

    # 真实项目写入 / 重新打开。
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["existing_cns_facilities"] = {
        "status": "passed", "count": 1, "items": [normalized],
    }
    workflow.session.save()
    reopened = WorkflowService(tmp_path / "project.json", DEFAULTS)
    stored = reopened.state["existing_cns_facilities"]["items"][0]["devices"][0]
    assert stored["service_key"] == SERVICE_KEY_RID_COOPERATIVE
    assert stored["coverage_geometry"]["radius_by_surface"] == RID_RADIUS_BY_SURFACE
    # catalog 不重复提供该设备：内联 RID 依然成立。
    catalog_items = (reopened.state.get("device_catalog") or {}).get("items") or []
    assert not any(str(item.get("device_id")) == "INLINE-RID" for item in catalog_items)
    assert is_rid_declaration(stored) is True


def test_legacy_existing_facility_device_shape_unchanged():
    """旧 fixture（无新服务字段）normalize 后仍保持 legacy 字段集与语义。"""

    normalized = normalize_existing_facility({
        "facility_id": "F1", "site_id": "S1", "coordinate": [0.0, 0.0],
        "vertical_profile": vertical(),
        "devices": [{"device_id": "D1", "name": "d1", "subsystem": "C", "status": "active",
                     "elevation_m": 10.0}],
        "status": "active", "source": "test",
    })
    device = normalized["devices"][0]
    assert "service_key" not in device
    assert "service_subtype" not in device
    assert device["type"]["technology"] == "unknown"
    assert device["coverage_geometry"]["model"] == "none"
    assert device["coverage_geometry"]["slant_range_m"] is None
    assert "radius_by_surface" not in device["coverage_geometry"]


# ---------------------------------------------------------------------------
# 5/6/13/14/15. CandidateAction / TowerColocation
# ---------------------------------------------------------------------------


def tower_candidates(*towers):
    policy = normalize_tower_colocation_policy({
        "planning_host_use_confirmed": True, "confirmed": True,
        "service_origin_assumption": "tower_top_agl_0",
    })
    profiles = {
        tower["tower_id"]: {"status": "resolved", "tower_top_orthometric_m": 100.0}
        for tower in towers
    }
    return build_tower_colocation_candidates(
        list(towers), obstacle_profiles={"items": profiles}, policy=policy,
    )


def real_tower(identifier, coordinate):
    return {
        "tower_id": identifier, "name": identifier,
        "longitude": coordinate[0], "latitude": coordinate[1],
        "site_type": "tower", "elevation_m": 0.0, "height_m": 100.0, "source": "test",
    }


def test_radius_by_surface_device_passes_candidate_geometry_gate():
    catalog = {"status": "passed", "items": [rid_device("RID1"), communication_device("COM1")]}
    actions = candidate_actions(
        [{"subsystem": "C"}, {"subsystem": "S"}], {}, {}, catalog,
        tower_candidates(real_tower("T-1", (0.0005, 0.0005))),
    )
    assert actions, "surface-aware device 必须能生成 candidate action"
    for action in actions:
        reasons = (action["eligibility"] or {}).get("reasons") or []
        assert not any("coverage geometry" in reason for reason in reasons), reasons
        device = next(item for item in catalog["items"] if item["device_id"] == action["device_id"])
        assert device["coverage_geometry"]["slant_range_m"] is None
    rid_action = next(item for item in actions if item["service_key"] == SERVICE_KEY_RID_COOPERATIVE)
    assert rid_action["eligibility"]["status"] == "eligible"
    assert rid_action["distinct_site_id"] == "tower:T-1"


def test_tower_colocation_distinct_site_id_is_stable_and_honest():
    candidates = tower_candidates(
        real_tower("T-A", (0.0005, 0.0005)), real_tower("T-B", (0.0015, 0.0015)),
    )
    catalog = {"status": "passed", "items": [rid_device("RID1"), communication_device("COM1")]}
    actions = candidate_actions(
        [{"subsystem": "C"}, {"subsystem": "S"}], {}, candidates, catalog,
    )
    identities = {(action["site_id"], action["service_key"]): action["distinct_site_id"]
                  for action in actions}
    # 同一塔 x 两个 service => 同一个物理站址身份（可共塔安装）。
    assert identities[("tower-colocation:T-A", SERVICE_KEY_RID_COOPERATIVE)] == "tower:T-A"
    assert identities[("tower-colocation:T-A", SERVICE_KEY_COMMUNICATION)] == "tower:T-A"
    assert identities[("tower-colocation:T-B", SERVICE_KEY_RID_COOPERATIVE)] == "tower:T-B"
    assert identities[("tower-colocation:T-B", SERVICE_KEY_COMMUNICATION)] == "tower:T-B"

    # 没有实际站址身份时：绝不编造（既不用 device_id，也不用坐标）。
    assert distinct_site_id_for({}, {"device_id": "X", "coordinate": [1.0, 2.0]}) is None


def test_same_tower_two_devices_never_count_as_two_distinct_sites():
    from cns_planner.algorithms.service_capability.v1 import (
        distinct_site_provider_count, summarize_service_redundancy,
    )

    def entry(site, device_id):
        return {
            "facility_id": "F", "device_id": device_id, "status": "meets_under_model",
            "subsystem": "S", "service_key": SERVICE_KEY_RID_COOPERATIVE,
            "service_surface_dependent": True, "distinct_site_id": site,
            "stage": "provider_service",
        }

    required = normalize_required_cns({
        "status": "passed",
        "project_default": {"surveillance": {
            "required": True, "status": "passed",
            "type": {"technology": "network_remote_id"},
            "performance": {},
            "service_key": SERVICE_KEY_RID_COOPERATIVE,
            "redundancy_by_surface": {"land": 2, "coastal_uncertain": 2, "sea": 1},
        }},
        "route_overrides": {},
    })["project_default"]["surveillance"]

    # 同塔两台 RID => distinct count = 1（不足 2）。
    same_tower = [entry("tower:T-A", "R1"), entry("tower:T-A", "R2")]
    assert distinct_site_provider_count(same_tower) == 1
    summary = summarize_service_redundancy(same_tower, required, surface_class="land")
    assert summary[0]["status"] == "confirmed_deficit"
    assert summary[0]["required_distinct_site_count"] == 2
    assert summary[0]["distinct_site_count"] == 1

    # 两座不同塔 => 2，land 满足。
    two_towers = [entry("tower:T-A", "R1"), entry("tower:T-B", "R2")]
    assert summarize_service_redundancy(
        two_towers, required, surface_class="land"
    )[0]["status"] == "satisfied"


def test_communication_and_rid_never_share_a_redundancy_pool():
    from cns_planner.algorithms.service_capability.v1 import summarize_service_redundancy

    entries = [
        {"facility_id": "F1", "device_id": "C1", "status": "meets_under_model",
         "subsystem": "C", "service_key": SERVICE_KEY_COMMUNICATION,
         "service_surface_dependent": True, "distinct_site_id": "tower:T-A",
         "stage": "provider_service"},
        {"facility_id": "F2", "device_id": "R1", "status": "meets_under_model",
         "subsystem": "S", "service_key": SERVICE_KEY_RID_COOPERATIVE,
         "service_surface_dependent": True, "distinct_site_id": "tower:T-B",
         "stage": "provider_service"},
    ]
    summary = summarize_service_redundancy(entries, {}, surface_class="land")
    pools = {item["service_key"]: item for item in summary}
    assert set(pools) == {SERVICE_KEY_COMMUNICATION, SERVICE_KEY_RID_COOPERATIVE}
    for key, pool in pools.items():
        assert pool["distinct_site_count"] == 1
        assert pool["required_distinct_site_count"] == 2
        # Communication Tower A + RID Tower B **绝不能**互相凑成 2 重。
        assert pool["status"] == "confirmed_deficit", key


# ---------------------------------------------------------------------------
# 7. 真实 land-mask surface facts
# ---------------------------------------------------------------------------


def test_real_land_mask_surface_facts_land_coastal_sea_unknown(tmp_path):
    land = land_mask_geojson(tmp_path)
    grid = grid_cells({
        "G-IN": (0.0002, 0.0002, 0.0008, 0.0008),
        "G-NEAR": (0.00200, 0.00080, 0.00204, 0.00084),
        "G-SEA": (0.0100, 0.0100, 0.0106, 0.0106),
    })
    facts, provider = provider_for(grid, land)
    assert facts["status"] == "passed"
    assert facts["semantics"] == "per_grid_cell_representative_point_surface_classification"
    assert facts["dem_nodata_used_to_infer_sea"] is False
    assert facts["unknown_is_fail_closed"] is True
    assert facts["land_mask"]["source_role"] == "land_mask"
    assert facts["land_mask"]["configured_path"].endswith("land.geojson")
    assert facts["classification_basis"].startswith("explicit_land_polygon_containment")
    assert facts["coastal_uncertainty"]["coastal_uncertainty_buffer_m"] == 30.0
    assert facts["by_grid_id"]["G-IN"] == "land"
    assert facts["by_grid_id"]["G-NEAR"] == "coastal_uncertain"
    assert facts["by_grid_id"]["G-SEA"] == "sea"
    assert facts["input_fingerprint"]

    # provider 与 classify_surface_detailed 的既有语义一致。
    assert provider.classify_surface(
        [[0.0005, 0.0005], [0.00202, 0.00082], [0.0103, 0.0103]]
    ) == ["land", "coastal_uncertain", "sea"]
    detailed = provider.classify_surface_detailed(0.00202, 0.00082)
    assert detailed["surface_class"] == "coastal_uncertain"
    assert detailed["effective_requirement_class"] == "land"
    assert detailed["required_distinct_site_count"] == 2
    # 落在没有 grid cell 的坐标 => unknown。
    assert provider.classify_surface([[5.0, 5.0]]) == ["unknown"]

    # 没有代表性坐标的格（无 bbox）保持 unknown，绝不猜测。
    no_center = grid_cells({"G-NO-CENTER": (0.0100, 0.0100, 0.0106, 0.0106)})
    no_center["cells"][0].pop("bbox")
    orphan = build_surface_class_facts(
        no_center, policy={"coastal_uncertainty_buffer_m": 30.0},
        land_mask_source=LandMaskSource(land, coastal_uncertainty_buffer_m=30.0),
    )
    assert orphan["by_grid_id"] == {"G-NO-CENTER": "unknown"}
    assert orphan["source"]["unresolved_cell_centers"] == 1


def test_missing_land_mask_keeps_every_cell_unknown_and_never_infers_sea(tmp_path):
    grid = grid_cells({"G-SEA-LOOKING": (0.0100, 0.0100, 0.0106, 0.0106)})
    facts = build_surface_class_facts(
        grid, policy={"coastal_uncertainty_buffer_m": 30.0},
        land_mask_source=None, land_mask_describe=None,
    )
    assert facts["by_grid_id"] == {"G-SEA-LOOKING": "unknown"}
    assert facts["source"]["land_mask_configured"] is False
    provider = SurfaceFactsProvider(facts, grid=grid)
    assert provider.classify_surface([[0.0103, 0.0103]]) == ["unknown"]


def test_surface_facts_provider_is_fail_closed_when_facts_stale(tmp_path):
    land = land_mask_geojson(tmp_path)
    grid = grid_cells({"G-IN": (0.0002, 0.0002, 0.0008, 0.0008)})
    facts, _ = provider_for(grid, land)
    stale = {**facts, "status": "stale"}
    provider = SurfaceFactsProvider(stale, grid=grid)
    assert provider.usable is False
    assert provider.classify_surface([[0.0005, 0.0005]]) == ["unknown"]
    assert surface_facts_input_fingerprint(stale) is None


# ---------------------------------------------------------------------------
# 8. fingerprint / invalidation
# ---------------------------------------------------------------------------


def test_surface_facts_and_policy_changes_stale_coverage_and_corridor(tmp_path):
    land = land_mask_geojson(tmp_path)
    grid = grid_cells({"G-IN": (0.0002, 0.0002, 0.0008, 0.0008)})
    facts, _ = provider_for(grid, land)

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["surface_class_facts"] = facts
    workflow.state["result_statuses"]["surface_class_facts"] = "passed"
    workflow.state["coverage_3d"] = {"status": "passed", "input_fingerprint": "F-COV"}
    workflow.state["result_statuses"]["coverage_3d"] = "passed"
    workflow.state["cns_service_capability"] = {"status": "passed", "input_fingerprint": "F-CAP"}
    workflow.state["result_statuses"]["cns_service_capability"] = "passed"
    workflow.state["cns_corridor_assessment"] = {"status": "passed", "input_fingerprint": "F-C14"}
    workflow.state["result_statuses"]["cns_corridor_assessment"] = "passed"
    workflow.state["cns_corridor_gap_assessment"] = {"status": "passed", "input_fingerprint": "F-C15"}
    workflow.state["result_statuses"]["cns_corridor_gap_assessment"] = "passed"
    workflow.state["cns_corridor_site_plan"] = {"status": "proposal_ready"}
    workflow.state["result_statuses"]["cns_corridor_site_plan"] = "passed"
    before_routes = deepcopy(workflow.state.get("operational_routes") or [])

    invalidated = workflow.invalidation_service.surface_facts_changed(
        "surface_class_facts_updated"
    )
    statuses = workflow.state["result_statuses"]
    for name in ("coverage_3d", "cns_service_capability", "cns_corridor_assessment",
                 "cns_corridor_gap_assessment", "cns_corridor_site_plan"):
        assert statuses[name] == "stale", name
    assert statuses["surface_class_facts"] == "stale"
    assert workflow.state["surface_class_facts"]["stale_reason"] == "surface_class_facts_updated"
    assert invalidated["operational_routes_untouched"] is True
    assert invalidated["theta_star_candidates_untouched"] is True
    assert invalidated["risk_untouched"] is True
    assert invalidated["radar_surveillance_layout_untouched"] is True
    assert (workflow.state.get("operational_routes") or []) == before_routes

    # 策略变化同样先让事实过时，再严格下游传播。
    workflow.state["coverage_3d"] = {"status": "passed", "input_fingerprint": "F-COV"}
    workflow.state["result_statuses"]["coverage_3d"] = "passed"
    workflow.invalidation_service.surface_classification_changed("policy_changed")
    assert workflow.state["result_statuses"]["coverage_3d"] == "stale"
    assert workflow.state["surface_class_facts"]["status"] == "stale"


def test_surface_facts_fingerprint_changes_input_fingerprints(tmp_path):
    land = land_mask_geojson(tmp_path)
    grid = grid_cells({"G-IN": (0.0002, 0.0002, 0.0008, 0.0008)})
    facts_a, provider_a = provider_for(grid, land)
    # 事实内容变化（模拟另一份陆域源：整块都是海）。
    facts_b = deepcopy(facts_a)
    facts_b["by_grid_id"] = {"G-IN": "sea"}
    facts_b["input_fingerprint"] = surface_facts_input_fingerprint(facts_b)
    assert facts_a["input_fingerprint"] != facts_b["input_fingerprint"]

    from cns_planner.algorithms.coverage.geometric_3d import GeometricCoverage3DV1
    from cns_planner.algorithms.corridor.v1 import CNSServiceCorridorV1

    coverage = GeometricCoverage3DV1()
    routes = [{"route_id": "R1", "status": "passed", "path": [[0.0, 0.0005], [0.002, 0.0005]]}]
    spatial = {"route_altitude_profiles": {"R1": {
        "route_id": "R1", "mode": "constant", "constant_altitude_m": 100.0,
        "vertical_reference": "egm2008_orthometric", "confirmed": True,
    }}, "altitude_layers": []}
    facilities = {"status": "passed", "count": 1, "items": [
        facility("F1", (0.001, 0.0005), [rid_device("RID1")]),
    ]}
    catalog = {"status": "passed", "items": [rid_device("RID1")]}
    coverage_fingerprints = [
        coverage.evaluate(
            routes, spatial, grid, {"terrain": {"cells": {}}}, facilities, catalog,
            surface_class_provider=provider_a, surface_facts_fingerprint=fingerprint,
        )["input_fingerprint"]
        for fingerprint in (
            facts_a["input_fingerprint"], facts_a["input_fingerprint"], facts_b["input_fingerprint"],
        )
    ]
    assert coverage_fingerprints[0] == coverage_fingerprints[1]
    assert coverage_fingerprints[0] != coverage_fingerprints[2], (
        "surface fact change must change Coverage input fingerprint"
    )

    corridor = CNSServiceCorridorV1()
    required = normalize_required_cns(requirement_set(
        service="surveillance",
        redundancy_by_surface={"land": 2, "coastal_uncertain": 2, "sea": 1},
        service_key=SERVICE_KEY_RID_COOPERATIVE,
    ))
    policy = normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": 0,
        "vertical_lower_margin_m": 10, "vertical_upper_margin_m": 10,
        "source": "test", "confirmed": True,
    }}})
    corridor_spatial = {**spatial, "altitude_layers": [{
        "altitude_layer_id": "L1", "lower_altitude_m": 50, "upper_altitude_m": 150,
        "vertical_reference": "egm2008_orthometric", "confirmed": True, "status": "confirmed",
    }]}
    corridor_fingerprints = [
        corridor.evaluate(
            routes, corridor_spatial, grid, {"terrain": {"cells": {}}},
            required, {}, facilities, catalog, policy,
            surface_class_provider=provider_a, surface_facts_fingerprint=fingerprint,
        )["input_fingerprint"]
        for fingerprint in (
            facts_a["input_fingerprint"], facts_a["input_fingerprint"], facts_b["input_fingerprint"],
        )
    ]
    assert corridor_fingerprints[0] == corridor_fingerprints[1]
    assert corridor_fingerprints[0] != corridor_fingerprints[2], (
        "surface fact change must change Corridor input fingerprint"
    )


# ---------------------------------------------------------------------------
# 9. Heavy task immutable snapshot 的可序列化性
# ---------------------------------------------------------------------------


def _assert_json_safe(value, path="inputs"):
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str), f"{path} key must be a string: {key!r}"
            _assert_json_safe(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_json_safe(item, f"{path}[{index}]")
        return
    raise AssertionError(f"{path} contains a non-serializable object: {type(value)!r}")


def test_heavy_task_snapshot_has_no_callable_or_qgis_object(tmp_path):
    land = land_mask_geojson(tmp_path)
    grid = grid_cells({"G-IN": (0.0002, 0.0002, 0.0008, 0.0008)})
    facts, _ = provider_for(grid, land)

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["grid"] = grid
    workflow.state["surface_class_facts"] = facts
    workflow.state["result_statuses"]["surface_class_facts"] = "passed"
    workflow.state["operational_routes"] = [{
        "route_id": "R1", "status": "passed", "path": [[0.0, 0.0005], [0.002, 0.0005]],
    }]
    service = CNSCorridorService(
        workflow.session, workflow.corridor_model,
        workflow.invalidation_service, workflow.snapshot,
    )
    inputs = service.compute_input()
    # (1) 同步 inputs 只携带**可序列化事实**，绝不含 provider / QGIS 对象。
    assert "surface_class_provider" not in inputs
    snapshot_payload = {
        "inputs": {key: value for key, value in inputs.items()
                   if key != "surface_class_provider"},
        "worker_payload": {},
    }
    _assert_json_safe(snapshot_payload)
    # (2) 真实序列化 / 反序列化后 surface facts 完全一致。
    blob = serialize_payload(snapshot_payload)
    restored = deserialize_payload(blob)
    assert restored["inputs"]["surface_class_facts"] == facts
    assert restored["inputs"]["surface_facts_fingerprint"] == facts["input_fingerprint"]
    # (3) 冻结进任务 immutable snapshot 的也是纯数据（task_specs 的正式输入组装）。
    from cns_planner.tasks.task_specs import _corridor_inputs

    frozen = _corridor_inputs(workflow.state, {})
    _assert_json_safe(frozen)
    assert frozen["surface_class_facts"] == facts
    assert frozen["surface_facts_fingerprint"] == facts["input_fingerprint"]
    # (4) worker 侧只用快照事实就能重建 provider。
    provider = SurfaceFactsProvider(restored["inputs"]["surface_class_facts"], grid=grid)
    assert provider.classify_surface([[0.0005, 0.0005]]) == ["land"]


# ---------------------------------------------------------------------------
# 10-12. 完整 Application 调用链的 surface 语义
# ---------------------------------------------------------------------------


def _coverage_for(workflow, provider, facts, route="R1"):
    """把可序列化 surface facts 写进 canonical state，再走**正式** P7 入口。

    P7 与 P14 因此消费同一份 facts（provider 由 state 现场构建），这正是 Round 2
    的 Application 接线口径：facts 是事实，provider 是它的运行期视图。
    """

    workflow.state["surface_class_facts"] = facts
    workflow.state["result_statuses"]["surface_class_facts"] = (
        "passed" if facts.get("status") == "passed" else "missing_data"
    )
    workflow.evaluate_coverage_3d()


def _configure(tmp_path, *, devices, facilities, radius_surface, services=None):
    service = "surveillance" if radius_surface is RID_RADIUS_BY_SURFACE else "communication"
    tmp_path.mkdir(parents=True, exist_ok=True)
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    origin_lat = 0.0005
    workflow.state["operational_routes"] = [{
        "route_id": "R1", "status": "passed",
        "path": [[0.0, origin_lat], [0.02, origin_lat]],
    }]
    workflow.state["grid"] = route_grid()
    workflow.state["grid_attributes"]["terrain"] = {"status": "passed", "cells": {}}
    workflow.state["spatial_3d"] = {
        "altitude_layers": [{
            "altitude_layer_id": "L1", "lower_altitude_m": 50, "upper_altitude_m": 150,
            "vertical_reference": "egm2008_orthometric", "source": "test",
            "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant", "constant_altitude_m": 100.0,
            "vertical_reference": "egm2008_orthometric", "source": "test",
            "confirmed": True, "status": "confirmed",
        }}, "site_vertical_profiles": {},
    }
    #: RequiredCNS 的 completeness 是 legacy 契约的一部分：surface 冗余由
    #: ``redundancy_by_surface`` 承载，但既有必填项仍需显式给出，否则 ``status``
    #: 会是 pending_confirmation（P8 只能给 unknown）。
    target_services = list(services or [service])
    requirement_payload = {
        "status": "passed",
        "project_default": {
            "communication": {"required": False, "status": "passed", "type": {}, "performance": {}},
            "navigation": {"required": False, "status": "passed", "type": {}, "performance": {}},
            "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
        },
        "route_overrides": {},
    }
    for name in target_services:
        single = requirement_set(
            service=name,
            redundancy_by_surface={"land": 2, "coastal_uncertain": 2, "sea": 1},
            service_key=(SERVICE_KEY_RID_COOPERATIVE if name == "surveillance"
                         else SERVICE_KEY_COMMUNICATION),
        )
        entry = single["project_default"][name]
        entry.update({
            "coverage_requirement": 1.0, "max_gap_m": 1000.0, "redundancy": 1,
        })
        if name == "surveillance":
            entry["update_interval_s"] = 5.0
        requirement_payload["project_default"][name] = entry
    workflow.state["required_cns"] = normalize_required_cns(requirement_payload)
    for name in target_services:
        assert workflow.state["required_cns"]["project_default"][name]["status"] == "passed"
    workflow.state["aircraft_profiles"] = {"status": "passed", "items": [normalize_aircraft_profile({
        "aircraft_id": "A1", "name": "A1",
        "communication": {"confirmed": True, "status": "confirmed", "capabilities": ["radio"],
                          "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
                          "performance": {"max_latency_s": 0.2, "min_redundancy": 1}},
        "navigation": {},
        #: 机载 RID 能力必须声明与 RequiredCNS 同一套类型事实（``target_cooperation``
        #: 与 ``max_update_interval_s``）：P4 匹配器把它们当作**需求**逐项核对，
        #: 缺失就是"证据不足"，不会静默通过。
        "surveillance": {"confirmed": True, "status": "confirmed", "capabilities": ["rid"],
                         "type": {"technology": "network_remote_id",
                                  "target_cooperation": "cooperative",
                                  "service_subtype": "cooperative_surveillance"},
                         "performance": {"min_redundancy": 1, "max_update_interval_s": 5.0}},
    })]}
    workflow.state["selected_aircraft_profile_id"] = "A1"
    workflow.state["device_catalog"] = {"status": "passed", "items": list(devices)}
    workflow.state["existing_cns_facilities"] = {
        "status": "passed", "count": len(facilities), "items": list(facilities),
    }
    workflow.state["cns_corridor_policy"] = normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": 0,
        "vertical_lower_margin_m": 10, "vertical_upper_margin_m": 10,
        "source": "test", "confirmed": True,
    }}})
    workflow.state["cns_planning_objectives"] = {
        "status": "pending_confirmation", "routes": {}, "source": None,
    }
    return workflow


def _land_and_sea_facts(tmp_path, workflow):
    """真实 land mask => 西段 land、东段 sea（由小格格心代表点自然得出）。"""

    tmp_path.mkdir(parents=True, exist_ok=True)
    land = land_mask_geojson(tmp_path)
    facts, provider = provider_for(workflow.state["grid"], land)
    by_id = facts["by_grid_id"]
    assert by_id["G01"] == "land", by_id
    assert by_id["G31"] == "sea", by_id
    assert {"land", "sea"} <= set(by_id.values()), by_id
    return facts, provider


def _run_corridor_chain(workflow, facts, provider):
    _coverage_for(workflow, provider, facts)
    workflow.evaluate_cns_corridor()
    workflow.evaluate_cns_corridor_gap()
    # 注意：``snapshot()`` 会把明细外置到 artifact；断言一律读 canonical state。
    return workflow.state["cns_corridor_gap_assessment"]


def _pools_by_surface(gap, subsystem, service_key):
    """按 surface 收集该 service 的独立冗余池结论（Communication / RID 各自成池）。"""

    result = {}
    for route in gap.get("routes") or []:
        for voxel in route.get("voxels") or []:
            for entry in voxel.get("subsystems") or []:
                if entry.get("subsystem") != subsystem:
                    continue
                for pool in entry.get("services") or []:
                    if pool.get("service_key") != service_key:
                        continue
                    result.setdefault(pool["surface_class"], []).append(pool)
    return result


def _pool_statuses(pools, surface):
    return {item["status"] for item in pools[surface]}


def test_rid_3km_land_not_covered_and_3km_sea_covered():
    providers = [{
        "facility_id": "F1", "device_id": "RID1", "coordinate": [0.0, 0.0005],
        "service_origin_egm2008_m": 100.0,
        "coverage_geometry": rid_device("RID1")["coverage_geometry"],
        "subsystem": "S", "service_key": SERVICE_KEY_RID_COOPERATIVE,
        "service_surface_dependent": True, "distinct_site_id": "site:F1",
    }]
    probe_land = {
        "longitude": longitude_offset(3000.0), "latitude": 0.0005,
        "altitude_egm2008_m": 100.0, "vertical_status": "passed", "surface_class": "land",
    }
    probe_sea = {**probe_land, "surface_class": "sea"}
    assert evaluate_geometry_point(probe_land, providers)["covered"] is False
    assert evaluate_geometry_point(probe_sea, providers)["covered"] is True
    # unknown 仍然 fail-closed（既不覆盖也不判负）。
    assert evaluate_geometry_point(
        {**probe_land, "surface_class": "unknown"}, providers
    )["covered"] is None


def test_communication_land_one_site_covers_but_redundancy_short(tmp_path):
    workflow = _configure(
        tmp_path, devices=[communication_device("COM1")],
        facilities=[facility("F1", (0.0, 0.0005), [communication_device("COM1")])],
        radius_surface=COMMUNICATION_RADIUS_BY_SURFACE,
    )
    facts, provider = _land_and_sea_facts(tmp_path, workflow)
    gap = _run_corridor_chain(workflow, facts, provider)
    pools = _pools_by_surface(gap, "C", SERVICE_KEY_COMMUNICATION)
    assert set(pools) == {"land", "sea"}, pools.keys()
    # land：单站 = 几何覆盖有、冗余不足（要求 2 个不同物理站址）。
    assert _pool_statuses(pools, "land") == {"confirmed_deficit"}
    land = pools["land"][0]
    assert land["required_distinct_site_count"] == 2
    assert land["distinct_site_count"] == 1
    # sea：单站即满足（要求 1）。
    assert _pool_statuses(pools, "sea") == {"satisfied"}
    assert pools["sea"][0]["required_distinct_site_count"] == 1

    # 几何覆盖本身是成立的：land 格有 provider 命中。
    coverage = workflow.state["coverage_3d"]["routes"][0]
    c_entry = next(item for item in coverage["subsystems"] if item["subsystem"] == "C")
    assert c_entry["status"] == "passed"
    assert c_entry["covered_fraction"] == 1.0
    assert {item["surface_class"] for item in c_entry["samples"]} == {"land", "sea"}


def test_communication_land_two_distinct_sites_satisfied(tmp_path):
    workflow = _configure(
        tmp_path, devices=[communication_device("COM1"), communication_device("COM2")],
        facilities=[
            facility("F1", (0.0, 0.0005), [communication_device("COM1")]),
            facility("F2", (0.0015, 0.0005), [communication_device("COM2")]),
        ],
        radius_surface=COMMUNICATION_RADIUS_BY_SURFACE,
    )
    facts, provider = _land_and_sea_facts(tmp_path, workflow)
    gap = _run_corridor_chain(workflow, facts, provider)
    pools = _pools_by_surface(gap, "C", SERVICE_KEY_COMMUNICATION)
    assert _pool_statuses(pools, "land") == {"satisfied"}
    assert pools["land"][0]["distinct_site_count"] == 2
    assert _pool_statuses(pools, "sea") == {"satisfied"}


def test_rid_land_single_site_deficit_two_sites_satisfied(tmp_path):
    single = _configure(
        tmp_path / "single", devices=[rid_device("RID1")],
        facilities=[facility("F1", (0.0, 0.0005), [rid_device("RID1")])],
        radius_surface=RID_RADIUS_BY_SURFACE,
    )
    facts, provider = _land_and_sea_facts(tmp_path / "single", single)
    gap = _run_corridor_chain(single, facts, provider)
    pools = _pools_by_surface(gap, "S", SERVICE_KEY_RID_COOPERATIVE)
    assert _pool_statuses(pools, "land") == {"confirmed_deficit"}
    assert pools["land"][0]["distinct_site_count"] == 1
    # 只有一座塔/站：sea 单站即可满足。
    assert _pool_statuses(pools, "sea") == {"satisfied"}

    double = _configure(
        tmp_path / "double", devices=[rid_device("RID1"), rid_device("RID2")],
        facilities=[
            facility("F1", (0.0, 0.0005), [rid_device("RID1")]),
            facility("F2", (0.0015, 0.0005), [rid_device("RID2")]),
        ],
        radius_surface=RID_RADIUS_BY_SURFACE,
    )
    facts2, provider2 = _land_and_sea_facts(tmp_path / "double", double)
    gap2 = _run_corridor_chain(double, facts2, provider2)
    pools2 = _pools_by_surface(gap2, "S", SERVICE_KEY_RID_COOPERATIVE)
    assert _pool_statuses(pools2, "land") == {"satisfied"}
    assert pools2["land"][0]["distinct_site_count"] == 2


def test_communication_and_rid_do_not_pool_together_in_application_chain(tmp_path):
    """同一项目里 Communication 与 RID 各自成池：绝不互相凑重数。"""

    workflow = _configure(
        tmp_path,
        devices=[communication_device("COM1"), rid_device("RID1")],
        facilities=[
            facility("F1", (0.0, 0.0005), [communication_device("COM1")]),
            facility("F2", (0.0015, 0.0005), [rid_device("RID1")]),
        ],
        radius_surface=COMMUNICATION_RADIUS_BY_SURFACE,
        services=["communication", "surveillance"],
    )
    facts, provider = _land_and_sea_facts(tmp_path, workflow)
    gap = _run_corridor_chain(workflow, facts, provider)
    comm = _pools_by_surface(gap, "C", SERVICE_KEY_COMMUNICATION)
    rid = _pools_by_surface(gap, "S", SERVICE_KEY_RID_COOPERATIVE)
    # Communication 只有 1 个物理站址 => land 仍不足；RID 同理。
    assert _pool_statuses(comm, "land") == {"confirmed_deficit"}
    assert comm["land"][0]["distinct_site_count"] == 1
    assert _pool_statuses(rid, "land") == {"confirmed_deficit"}
    assert rid["land"][0]["distinct_site_count"] == 1


def test_sea_single_site_is_satisfied_for_both_services(tmp_path):
    for label, radii, builder in (
        ("communication", COMMUNICATION_RADIUS_BY_SURFACE, communication_device),
        ("rid", RID_RADIUS_BY_SURFACE, rid_device),
    ):
        workdir = tmp_path / label
        workdir.mkdir(parents=True, exist_ok=True)
        workflow = _configure(
            workdir, devices=[builder("D1")],
            facilities=[facility("F1", (0.0005, 0.0005), [builder("D1")])],
            radius_surface=radii,
        )
        # 把陆域矩形挪到远处 => 覆盖点所在的格全部是 sea（真实分类结果，非硬编码）。
        facts, provider = provider_for(
            workflow.state["grid"],
            land_mask_geojson(workdir, name=f"{label}_far_land.geojson", offset=(10.0, 10.0)),
        )
        assert set(facts["by_grid_id"].values()) == {"sea"}, facts["by_grid_id"]
        gap = _run_corridor_chain(workflow, facts, provider)
        subsystem = "C" if label == "communication" else "S"
        service_key = (SERVICE_KEY_COMMUNICATION if label == "communication"
                       else SERVICE_KEY_RID_COOPERATIVE)
        pools = _pools_by_surface(gap, subsystem, service_key)
        assert set(pools) == {"sea"}, pools.keys()
        assert pools["sea"][0]["required_distinct_site_count"] == 1
        assert _pool_statuses(pools, "sea") == {"satisfied"}, (label, pools)


# ---------------------------------------------------------------------------
# 16. 旧 C/N/S fixture 行为兼容 + radius_m 兼容语义
# ---------------------------------------------------------------------------


def _legacy_workflow(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["operational_routes"] = [{
        "route_id": "R1", "status": "passed", "path": [[0.0, 0.0005], [0.002, 0.0005]],
    }]
    workflow.state["grid"] = grid_cells({"G1": (0.0, 0.0, 0.001, 0.001)})
    workflow.state["grid_attributes"]["terrain"] = {"status": "passed", "cells": {}}
    workflow.state["spatial_3d"] = {
        "altitude_layers": [{
            "altitude_layer_id": "L1", "lower_altitude_m": 50, "upper_altitude_m": 150,
            "vertical_reference": "egm2008_orthometric", "source": "test",
            "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant", "constant_altitude_m": 100.0,
            "vertical_reference": "egm2008_orthometric", "source": "test",
            "confirmed": True, "status": "confirmed",
        }}, "site_vertical_profiles": {},
    }
    workflow.state["required_cns"] = normalize_required_cns({
        "status": "passed",
        "project_default": {
            "communication": {"required": True, "status": "passed",
                              "coverage_requirement": 1.0, "max_gap_m": 1000.0,
                              "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
                              "performance": {"max_latency_s": 0.5, "min_redundancy": 1}},
            "navigation": {"required": False, "status": "passed", "type": {}, "performance": {}},
            "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
        },
        "route_overrides": {},
    })
    workflow.state["aircraft_profiles"] = {"status": "passed", "items": [{
        "aircraft_id": "A1", "name": "A1",
        "communication": {"confirmed": True, "status": "confirmed", "capabilities": ["radio"],
                          "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
                          "performance": {"max_latency_s": 0.2, "min_redundancy": 1}},
        "navigation": {}, "surveillance": {},
    }]}
    workflow.state["selected_aircraft_profile_id"] = "A1"
    workflow.state["device_catalog"] = {"status": "passed", "items": [legacy_device("LEG1")]}
    workflow.state["existing_cns_facilities"] = {
        "status": "passed", "count": 1,
        "items": [facility("F1", (0.0, 0.0005), [legacy_device("LEG1")])],
    }
    workflow.state["cns_corridor_policy"] = normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": 0,
        "vertical_lower_margin_m": 10, "vertical_upper_margin_m": 10,
        "source": "test", "confirmed": True,
    }}})
    return workflow


def _legacy_provider_evidence_fields():
    return {
        "facility_id", "device_id", "slant_distance_m", "horizontal_distance_m",
        "vertical_delta_m", "geometry_model",
    }


def test_legacy_chain_behaves_identically_without_surface_facts(tmp_path):
    workflow = _legacy_workflow(tmp_path)
    # surface facts 完全缺失（旧项目）：legacy slant_range_m 语义照旧。
    assert workflow.state["surface_class_facts"]["status"] == "not_calculated"
    workflow.evaluate_coverage_3d()
    coverage = workflow.state["coverage_3d"]
    c_entry = next(
        item for item in coverage["routes"][0]["subsystems"] if item["subsystem"] == "C"
    )
    assert c_entry["status"] == "passed"
    assert c_entry["covered_fraction"] == 1.0
    sample = c_entry["samples"][0]
    # surface_class 缺失即 unknown（不猜测、不回落 land/sea）。
    assert sample["surface_class"] == "unknown"
    # legacy provider 证据不因 Round 2 膨胀（P14 payload 限制）。
    assert set(sample["providers"][0]) == _legacy_provider_evidence_fields()

    workflow.evaluate_cns_corridor()
    workflow.evaluate_cns_corridor_gap()
    voxel = workflow.state["cns_corridor_gap_assessment"]["routes"][0]["voxels"][0]
    assert voxel["surface_class"] == "unknown"
    c_voxel = next(item for item in voxel["subsystems"] if item["subsystem"] == "C")
    # legacy 子系统级判定完全不变：min_redundancy=1 且已覆盖 => satisfied。
    assert c_voxel["combined_status"] == "satisfied"
    assert c_voxel["services"] == [], "legacy 项目不得凭空产生 service 级冗余池"
    # P14 provider 证据同样保持 legacy 字段集：service 维度只在显式新服务时出现。
    # （P15 把 P14 的原始 provider 证据放在 evidence[0] 里。）
    p14_evaluations = c_voxel["evidence"][0]["provider_evaluations"]
    assert "service_key" not in p14_evaluations[0]
    assert "distinct_site_id" not in p14_evaluations[0]


def test_no_cross_service_pool_appears_for_legacy_projects(tmp_path):
    workflow = _legacy_workflow(tmp_path)
    workflow.evaluate_coverage_3d()
    workflow.evaluate_cns_corridor()
    workflow.evaluate_cns_corridor_gap()
    gap = workflow.state["cns_corridor_gap_assessment"]
    for route in gap["routes"]:
        for voxel in route["voxels"]:
            for entry in voxel["subsystems"]:
                assert entry["services"] == []
    for route in gap["routes"]:
        for summary in route["subsystems"]:
            assert summary["service_redundancy"] == []


def test_surface_facts_normalization_is_idempotent():
    facts = empty_surface_class_facts()
    assert normalize_surface_class_facts(facts) == facts
    populated = {
        **facts, "status": "passed",
        "by_grid_id": {"G1": "land", "G2": "sea", "G3": "bogus"},
        "grid_cell_count": 3,
    }
    once = normalize_surface_class_facts(populated)
    assert once["by_grid_id"] == {"G1": "land", "G2": "sea", "G3": "unknown"}
    assert once["surface_class_counts"]["unknown"] == 1
    assert normalize_surface_class_facts(once) == once


def test_radius_m_never_overrides_surface_authoritative_geometry():
    """B5：单一 ``radius_m`` 只是 compatibility-only，绝不替代 radius_by_surface。"""

    device = rid_device("RID1")
    geometry = device["coverage_geometry"]
    # 显式把 legacy 单一半径设成 5 km，也不得改变 land 下的 authoritative 半径。
    device["radius_m"] = 5000.0
    geometry["slant_range_m"] = 5000.0
    assert effective_radius_m(geometry, "land")["radius_m"] == RID_RADIUS_BY_SURFACE["land"]
    assert effective_radius_m(geometry, "sea")["radius_m"] == RID_RADIUS_BY_SURFACE["sea"]
    assert effective_radius_m(geometry, "land")["source"] == "radius_by_surface"
    assert effective_radius_m(geometry, "unknown")["radius_m"] is None

    # 旧设备（只有 slant_range_m）保持 legacy 语义：任何 surface 都用它。
    legacy_geometry = {"model": "hemisphere", "slant_range_m": 2000.0}
    for surface in ("land", "coastal_uncertain", "sea"):
        resolution = effective_radius_m(legacy_geometry, surface)
        assert resolution["radius_m"] == 2000.0
        assert resolution["source"] == "legacy_slant_range_m"


def test_candidate_site_normalization_still_requires_explicit_fields():
    """CandidateSite 归一化未被本轮放宽：可判定性仍来自显式字段。"""

    site = normalize_candidate_site({
        "site_id": "S1", "coordinate": [0.0, 0.0], "available_subsystems": ["C", "S"],
        "planning_profile": {"reuse_class": "candidate_site", "add_device_allowed": True,
                             "source": "test", "confirmed": True},
    })
    assert site["available_subsystems"] == ["C", "S"]
    assert site["planning_profile"]["reuse_class"] == "candidate_site"


# ---------------------------------------------------------------------------
# 17. Round 2 P0 收口：surface policy 与 Radar policy 的运行期解耦
#     A. 旧项目一次性迁移（含 provenance）
#     B. 迁移后改 Radar policy 不影响 surface policy / facts fingerprint
#     C. 改中立 surface policy 让 surface facts 过时且指纹改变
#     D. 新项目无需 Radar layout / Radar policy 运行态即可生成 surface facts
# ---------------------------------------------------------------------------


LEGACY_RADAR_LAYER = "legacy_land_mask_layer"
LEGACY_RADAR_BUFFER_M = 45.0


def legacy_project_without_surface_policy(tmp_path, *, name="legacy_project.json"):
    """写一份**旧项目**：Radar policy 已配置，但**完全没有** surface policy。

    Round 2 P0 之前的持久化状态就是这样：surface classification 只有"从 Radar
    policy 借用"的隐藏耦合，没有任何独立的中立策略键。
    """

    path = tmp_path / name
    workflow = WorkflowService(path, DEFAULTS)
    workflow.set_radar_surveillance_policy({
        "coastal_uncertainty_buffer_m": LEGACY_RADAR_BUFFER_M,
        "land_mask_layer_name": LEGACY_RADAR_LAYER,
        "confirmed": True,
    })
    workflow.session.save()
    document = json.loads(path.read_text(encoding="utf-8"))
    assert "surface_classification_policy" in document
    document.pop("surface_classification_policy")
    document.pop("surface_class_facts", None)
    (document.get("result_statuses") or {}).pop("surface_class_facts", None)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def test_legacy_project_migrates_surface_policy_once_with_provenance(tmp_path):
    """A：旧项目只有 Radar policy、无 surface policy → 一次性迁移出独立策略。"""

    path = legacy_project_without_surface_policy(tmp_path)
    # 迁移前：持久化状态中确实没有 surface policy（这就是"完全没有"的判据）。
    assert "surface_classification_policy" not in json.loads(
        path.read_text(encoding="utf-8")
    )

    reopened = WorkflowService(path, DEFAULTS)
    policy = reopened.state["surface_classification_policy"]
    radar = reopened.state["radar_surveillance_policy"]

    # (1) 生成了**独立**的中立策略，参数与旧项目当前值逐字段一致。
    assert policy["origin"] == POLICY_ORIGIN_LEGACY_RADAR_MIGRATION
    assert policy["source"] == POLICY_ORIGIN_LEGACY_RADAR_MIGRATION
    assert policy["land_mask_layer_name"] == LEGACY_RADAR_LAYER
    assert policy["coastal_uncertainty_buffer_m"] == LEGACY_RADAR_BUFFER_M
    assert policy["land_mask_layer_name"] == radar["land_mask_layer_name"]
    assert policy["coastal_uncertainty_buffer_m"] == radar["coastal_uncertainty_buffer_m"]
    assert policy["semantics"] == "step5_shared_neutral_land_mask_classification_policy"
    # (2) 溯源显式记录这是一次性 legacy 迁移，绝不冒充工程默认。
    provenance = policy["legacy_radar_policy_migration"]
    assert provenance["origin"] == POLICY_ORIGIN_LEGACY_RADAR_MIGRATION
    assert provenance["one_shot"] is True
    assert provenance["source_policy_key"] == "radar_surveillance_policy"
    assert set(provenance["copied_fields"]) == {
        "land_mask_layer_name", "coastal_uncertainty_buffer_m",
    }

    # (3) 一次性：保存并重开后不再迁移（策略与溯源都稳定，不随 Radar 漂移）。
    reopened.session.save()
    again = WorkflowService(path, DEFAULTS)
    assert again.state["surface_classification_policy"] == policy


def test_radar_policy_change_after_migration_never_touches_surface(tmp_path):
    """B：迁移完成后修改 Radar coastal buffer → surface policy / facts 指纹不变。"""

    path = legacy_project_without_surface_policy(tmp_path)
    land = land_mask_geojson(tmp_path)
    workflow = WorkflowService(path, DEFAULTS)
    workflow.state["grid"] = grid_cells({"G-IN": (0.0002, 0.0002, 0.0008, 0.0008)})
    workflow.surface_classification_land_mask_path = lambda: str(land)
    facts_before = workflow.update_surface_class_facts()
    assert facts_before["status"] == "passed"
    policy_before = deepcopy(workflow.state["surface_classification_policy"])
    assert policy_before["coastal_uncertainty_buffer_m"] == LEGACY_RADAR_BUFFER_M
    # 分类参数确实生效（45 m 海岸不确定带）。
    assert facts_before["coastal_uncertainty"]["coastal_uncertainty_buffer_m"] == (
        LEGACY_RADAR_BUFFER_M
    )
    # Communication / RID 链处于活跃状态：修改 Radar policy 不得 stale 它们。
    for key in ("coverage_3d", "cns_service_capability", "cns_corridor_assessment",
                "cns_corridor_gap_assessment"):
        workflow.state[key] = {"status": "passed", "input_fingerprint": f"F-{key}"}
        workflow.state["result_statuses"][key] = "passed"

    # 改 Radar policy：只 stale Radar 自己的产物。
    workflow.set_radar_surveillance_policy({"coastal_uncertainty_buffer_m": 900.0})
    assert workflow.state["radar_surveillance_policy"][
        "coastal_uncertainty_buffer_m"
    ] == 900.0

    assert workflow.state["surface_classification_policy"] == policy_before
    assert workflow.state["surface_class_facts"]["input_fingerprint"] == (
        facts_before["input_fingerprint"]
    )
    assert workflow.state["result_statuses"]["surface_class_facts"] == "passed"
    for key in ("coverage_3d", "cns_service_capability", "cns_corridor_assessment",
                "cns_corridor_gap_assessment"):
        assert workflow.state["result_statuses"][key] == "passed", key

    # 落盘后重新打开：surface 侧完全不受 Radar policy 影响。
    workflow.session.save()
    reopened = WorkflowService(path, DEFAULTS)
    assert reopened.state["surface_classification_policy"] == policy_before
    assert reopened.state["surface_class_facts"]["input_fingerprint"] == (
        facts_before["input_fingerprint"]
    )
    assert reopened.state["radar_surveillance_policy"][
        "coastal_uncertainty_buffer_m"
    ] == 900.0


def test_surface_policy_change_stales_facts_and_changes_fingerprint(tmp_path):
    """C：修改中立 surface policy → surface facts stale / 指纹改变 + 严格下游失效。"""

    land = land_mask_geojson(tmp_path)
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["grid"] = grid_cells({"G-IN": (0.0002, 0.0002, 0.0008, 0.0008)})
    workflow.surface_classification_land_mask_path = lambda: str(land)
    workflow.set_surface_classification_policy({
        "coastal_uncertainty_buffer_m": 30.0, "confirmed": True,
    })
    facts_before = workflow.update_surface_class_facts()
    assert facts_before["status"] == "passed"
    assert facts_before["input_fingerprint"]

    # 让整条 surface 链处于"活跃"状态，才能验证策略变化的定向失效。
    for key, payload in (
        ("coverage_3d", {"status": "passed", "input_fingerprint": "F-COV"}),
        ("cns_service_capability", {"status": "passed", "input_fingerprint": "F-CAP"}),
        ("cns_corridor_assessment", {"status": "passed", "input_fingerprint": "F-C14"}),
        ("cns_corridor_gap_assessment", {"status": "passed", "input_fingerprint": "F-C15"}),
        ("cns_corridor_site_plan", {"status": "proposal_ready"}),
    ):
        workflow.state[key] = payload
        workflow.state["result_statuses"][key] = "passed"
    workflow.state["cns_planning_reports"] = {
        "status": "passed", "active_report_id": "RPT-1",
        "records": [{"report_id": "RPT-1", "current_applicability": "current_project"}],
    }
    workflow.state["result_statuses"]["report"] = "passed"

    workflow.set_surface_classification_policy({"coastal_uncertainty_buffer_m": 400.0})

    statuses = workflow.state["result_statuses"]
    for name in (
        "surface_class_facts", "coverage_3d", "cns_service_capability",
        "cns_corridor_assessment", "cns_corridor_gap_assessment",
        "cns_corridor_site_plan", "report",
    ):
        assert statuses[name] == "stale", name
    assert workflow.state["surface_class_facts"]["status"] == "stale"
    assert surface_facts_input_fingerprint(workflow.state["surface_class_facts"]) is None

    # 重新生成：策略（海岸不确定带）进入指纹，因此指纹必须改变。
    workflow.surface_classification_land_mask_path = lambda: str(land)
    facts_after = workflow.update_surface_class_facts()
    assert facts_after["status"] == "passed"
    assert facts_after["coastal_uncertainty"]["coastal_uncertainty_buffer_m"] == 400.0
    assert facts_after["input_fingerprint"] != facts_before["input_fingerprint"]


class _RadarRuntimeTrap:
    """Radar 运行时对象替身：任何属性访问都直接失败。

    用于证明 Communication / RID 的正式 surface classification 运行时**完全不依赖**
    Radar policy 或 Radar layout 的 ``facts_provider``。
    """

    def __getattr__(self, name):
        raise AssertionError(f"surface classification 不得访问 Radar 运行时对象：{name}")


def test_new_project_surface_facts_need_no_radar_runtime(tmp_path):
    """D：新项目不需要 Radar layout 或 Radar policy 运行态即可生成 surface facts。"""

    land = land_mask_geojson(tmp_path)
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    # 全新项目：Radar layout 从未运行，facts_provider 从未注入。
    assert workflow.radar_surveillance_layout_service.facts_provider is None
    assert workflow.state["result_statuses"]["radar_surveillance_layout"] == "not_calculated"
    assert workflow.state["surface_classification_policy"]["origin"] == (
        "project_engineering_default"
    )

    workflow.state["grid"] = grid_cells({"G-IN": (0.0002, 0.0002, 0.0008, 0.0008)})
    workflow.surface_classification_land_mask_path = lambda: str(land)
    workflow.set_surface_classification_policy({
        "coastal_uncertainty_buffer_m": 30.0, "confirmed": True,
    })
    # 用 trap 整体替换 Radar service：任何触碰都会立刻失败。
    workflow.radar_surveillance_layout_service = _RadarRuntimeTrap()

    facts = workflow.update_surface_class_facts()
    assert facts["status"] == "passed"
    assert facts["by_grid_id"] == {"G-IN": "land"}
    assert facts["policy"]["origin"] == "project_engineering_default"
    assert facts["land_mask"]["configured_path"].endswith("land.geojson")
    # provider 由**中立 facts** 重建，无需任何 Radar 运行态。
    provider = SurfaceFactsProvider(facts, grid=workflow.state["grid"])
    assert provider.classify_surface([[0.0005, 0.0005]]) == ["land"]

    # 没有 land-mask 数据源时 fail-closed（不是"全部是海"），同样不需要 Radar。
    workflow.surface_classification_land_mask_path = lambda: None
    missing = workflow.update_surface_class_facts()
    assert missing["by_grid_id"] == {"G-IN": "unknown"}
    assert missing["source"]["land_mask_configured"] is False


def _function_identifiers(path, name):
    """函数体（不含函数 docstring）里出现的全部标识符与字符串常量。

    用于**结构性**证明运行时路径不引用 Radar policy：注释可以提及它，代码不行。
    """

    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = list(node.body)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)):
                body = body[1:]
            names = set()
            for statement in body:
                for inner in ast.walk(statement):
                    if isinstance(inner, ast.Name):
                        names.add(inner.id)
                    elif isinstance(inner, ast.Attribute):
                        names.add(inner.attr)
                    elif isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        names.add(inner.value)
            return names
    raise AssertionError(f"{path} 中找不到函数 {name}")


def test_runtime_surface_paths_never_reference_radar_policy():
    """A/B/D 的结构性护栏：运行时 surface 路径的代码里没有 Radar policy 引用。"""

    forbidden = {
        "radar_surveillance_policy", "policy_snapshot",
        "radar_surveillance_layout_service", "facts_provider",
    }
    land_policy = _function_identifiers(
        "cns_planner/application/app_context.py", "_land_policy",
    )
    assert "surface_classification_policy" in land_policy
    assert not (forbidden & land_policy), forbidden & land_policy

    surface_update = _function_identifiers(
        "cns_planner/application/workflow_service.py", "update_surface_class_facts",
    )
    assert "surface_classification_policy" in surface_update
    assert "land_mask_source" in surface_update
    assert not (forbidden & surface_update), forbidden & surface_update

    # 迁移只允许出现在 domain 的迁移函数与 project_state 的 normalize 阶段。
    migration = _function_identifiers(
        "cns_planner/domain/surface_classification.py",
        "migrate_surface_classification_policy_from_legacy_radar_policy",
    )
    assert "LEGACY_RADAR_POLICY_KEY" in migration
    assert "land_mask_layer_name" in migration
    assert "coastal_uncertainty_buffer_m" in migration
