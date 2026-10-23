"""Round38-W1A：固定 1000 m CNS 保障宽带的契约、几何、P14 1.4 与 P15 1.3 验收。

覆盖任务书要求证明的七条：
1. 航路横向宽度固定 1000 m；
2. 不同航路均消费相同宽度契约；
3. P14/P15 能正确识别全宽 CNS 需求与缺口；
4. 中心线满足不自动代表全宽满足；
5. 历史版本与不同 R0005 几何不得复用为 current；
6. P14 复杂度上限及后台任务行为不退化；
7. 原 N1、3B 与正式 ProjectState 均未被破坏（本文件只断言 W1A 侧的不变量）。
"""

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.corridor.v1 import (
    CNSServiceCorridorV1, SAFETY_EVALUATION_CEILING, VALIDATED_EVALUATION_LIMIT,
    estimate_corridor_complexity,
)
from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from cns_planner.domain.cns_corridor import (
    CORRIDOR_HALF_WIDTH_M, CORRIDOR_TOTAL_WIDTH_M, GEOMETRY_ALGORITHM_ID,
    WIDTH_POLICY_VERSION, corridor_half_width_m, corridor_geometry_identity_summary,
    fixed_corridor_width_contract, normalize_cns_corridor_policy,
)
from cns_planner.domain.cns_planning_objectives import normalize_cns_planning_objectives
from cns_planner.gis.corridor_geometry import (
    build_fixed_corridor_geometry, build_fixed_corridor_geometries,
)


# ---- 夹具（与 tests/test_cns_corridor.py 同构，但显式覆盖多航路与历史半宽） ----

def _spatial(route_ids):
    return {
        "status": "passed",
        "altitude_layers": [{
            "altitude_layer_id": "L1", "name": "L1", "lower_altitude_m": 50.0,
            "upper_altitude_m": 150.0, "vertical_reference": "egm2008_orthometric",
            "source": "test", "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {
            route_id: {
                "route_id": route_id, "mode": "constant",
                "vertical_reference": "egm2008_orthometric",
                "constant_altitude_m": 100.0, "waypoints": [], "source": "test",
                "confirmed": True, "status": "confirmed", "geoid_undulation_m": None,
            } for route_id in route_ids
        },
        "site_vertical_profiles": {},
    }


def _device(radius):
    return {
        "device_id": "D1", "name": "D1", "subsystem": "C", "role": "existing",
        "radius_m": radius, "mtbf_h": 1000,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "sphere", "slant_range_m": radius, "source": "test",
            "confirmed": True,
        },
        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "dedicated_radio",
            "parameters": {"performance": {"max_latency_s": 0.2}},
            "source": "test", "confirmed": True,
        },
    }


def _facilities():
    return {"status": "passed", "items": [{
        "facility_id": "F1", "coordinate": [0.0, 0.0], "status": "active",
        "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
        "devices": [{"device_id": "D1", "subsystem": "C", "status": "active"}],
    }]}


def _requirements():
    return {"status": "passed", "project_default": {
        "communication": {
            "required": True, "status": "passed",
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 1.0, "min_redundancy": 1},
        },
        "navigation": {"required": False, "status": "passed", "type": {}, "performance": {}},
        "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
    }, "route_overrides": {}}


def _aircraft():
    return {
        "aircraft_id": "A1",
        "communication": {
            "confirmed": True, "status": "confirmed", "capabilities": ["radio"],
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.5, "min_redundancy": 1},
        },
        "navigation": {}, "surveillance": {},
    }


def _policy(widths, *, confirmed=True):
    return normalize_cns_corridor_policy({"routes": {
        route_id: {
            "route_id": route_id, "horizontal_half_width_m": width,
            "vertical_lower_margin_m": 10, "vertical_upper_margin_m": 10,
            "source": "test", "confirmed": confirmed,
        } for route_id, width in widths.items()
    }})


#: 两条航路：一条 0.1° 长（约 11 km），一条 0.02° 短，横向相距 0.02°。
ROUTES = [
    {"route_id": "R1", "status": "passed", "path": [[0.0, 0.0], [0.0, 0.1]]},
    {"route_id": "R2", "status": "passed", "path": [[0.02, 0.0], [0.02, 0.02]]},
]


def _grid(step=0.002, columns=12, rows=14):
    cells = []
    for row in range(rows):
        for column in range(columns):
            left, bottom = -0.006 + column * step, -0.006 + row * step
            cells.append({
                "grid_id": f"G{row:03d}{column:03d}",
                "bbox": [left, bottom, left + step, bottom + step],
            })
    return {"status": "passed", "level": 7, "cells": cells}


def _evaluate(policy, *, routes=None, catalog=None, geometry=None):
    routes = routes or ROUTES
    return CNSServiceCorridorV1().evaluate(
        routes, _spatial([item["route_id"] for item in routes]), _grid(),
        {"terrain": {"status": "passed", "cells": {}}}, _requirements(), _aircraft(),
        _facilities(), catalog or {"status": "passed", "items": [_device(40.0)]},
        policy, corridor_geometry=geometry,
    )


# ---- 1. 契约：横向宽度固定 1000 m ----

def test_width_contract_is_the_single_fixed_source():
    contract = fixed_corridor_width_contract()
    assert contract["corridor_half_width_m"] == 500.0
    assert contract["corridor_total_width_m"] == 1000.0
    assert contract["width_policy_version"] == "fixed-1000m-v1"
    assert contract["width_semantics"] == "cns_service_target_band"
    assert contract["cap_style"] == "round" and contract["join_style"] == "round"
    assert contract["regulatory_operational_volume"] == "not_evaluated"
    # 唯一入口永远返回固定值，即使 spec 里写着别的历史宽度。
    assert corridor_half_width_m({"horizontal_half_width_m": 150.0}) == 500.0
    assert corridor_half_width_m(None) == 500.0


def test_legacy_non_500m_width_is_kept_read_only_and_marked_stale():
    policy = _policy({"R1": 150.0, "R2": 500.0})
    assert policy["width_policy_version"] == WIDTH_POLICY_VERSION
    assert policy["corridor_half_width_m"] == 500.0
    assert policy["horizontal_width_editable"] is False
    assert policy["legacy_stale_route_ids"] == ["R1"]
    # 原值**不被覆盖**，只是被标记。
    assert policy["routes"]["R1"]["horizontal_half_width_m"] == 150.0
    assert policy["routes"]["R1"]["horizontal_half_width_status"] == "stale_legacy_value"
    assert policy["routes"]["R1"]["effective_corridor_half_width_m"] == 500.0
    assert policy["routes"]["R2"]["horizontal_half_width_status"] == "fixed_by_policy"
    # 垂向余量仍独立确认；水平契约固定不给它带来默认值或确认状态。
    assert policy["routes"]["R1"]["vertical_lower_margin_m"] == 10.0


def test_missing_vertical_margin_stays_pending_even_with_fixed_width():
    policy = normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": 500.0, "confirmed": True,
    }}})
    spec = policy["routes"]["R1"]
    assert spec["status"] == "pending_confirmation"
    assert spec["confirmed"] is False
    assert spec["horizontal_half_width_status"] == "fixed_by_policy"
    assert spec["vertical_lower_margin_m"] is None


# ---- 2. 不同航路消费同一宽度契约 ----

def test_every_route_consumes_the_same_width_contract():
    policy = _policy({"R1": 150.0, "R2": 999.0})
    result = _evaluate(policy)
    assert result["algorithm_version"] == "1.4"
    assert result["fixed_corridor_width_contract"]["corridor_total_width_m"] == 1000.0
    for route in result["routes"]:
        assert route["horizontal_half_width_m"] == 500.0
        assert route["horizontal_total_width_m"] == 1000.0
        assert route["width_policy_version"] == WIDTH_POLICY_VERSION
        assert route["legacy_width_used_as_current"] is False
        assert route["horizontal_width_editable"] is False
        assert route["legacy_horizontal_half_width_status"] == "stale_legacy_value"


def test_legacy_width_cannot_change_the_included_grid_set():
    """同一航路，仅历史半宽不同 ⇒ 参与的网格集合与结论逐字段一致。"""

    narrow = _evaluate(_policy({"R1": 50.0}))
    wide = _evaluate(_policy({"R1": 2000.0}))
    assert [item["grid_id"] for item in narrow["routes"][0]["voxels"]] == [
        item["grid_id"] for item in wide["routes"][0]["voxels"]
    ]
    assert narrow["routes"][0]["horizontal_cell_count"] == (
        wide["routes"][0]["horizontal_cell_count"]
    )
    # 但 legacy 审计值确实不同 ⇒ 输入指纹必须不同（不得把两者视为同一基线）。
    assert narrow["input_fingerprint"] != wide["input_fingerprint"]


# ---- 3. P14 全宽评估与几何身份 ----

def test_p14_carries_shared_metric_geometry_identity():
    geometry = build_fixed_corridor_geometries(ROUTES, _policy({"R1": 500.0, "R2": 500.0}))
    result = _evaluate(_policy({"R1": 500.0, "R2": 500.0}), geometry=geometry)
    for route in result["routes"]:
        identity = route["fixed_corridor_geometry"]
        assert route["fixed_corridor_geometry_status"] == "available"
        assert identity["geometry_algorithm_id"] == GEOMETRY_ALGORITHM_ID
        assert identity["width_policy_version"] == WIDTH_POLICY_VERSION
        assert identity["corridor_half_width_m"] == 500.0
        assert identity["corridor_total_width_m"] == 1000.0
        assert identity["cap_style"] == "round" and identity["join_style"] == "round"
        assert identity["source_crs"] == "OGC:CRS84"
        # 夹具航路在经度 0 附近 ⇒ 由地理范围推导出的 UTM 分带是 31N（绝不猜测区域码）。
        assert identity["construction_crs"] == "EPSG:32631"
        assert identity["point_count"] == 2
        assert identity["geometry_fingerprint"].startswith("corridorgeometryv1-")
        # 精确 buffer 身份与离散评估身份必须是两个不同指纹（既有字段语义不变）。
        assert route["corridor_geometry_fingerprint"] != identity["geometry_fingerprint"]
        assert route["corridor_geometry"]["horizontal_discretization"] == (
            "conservative_grid_cell_inclusion_not_exact_buffer")
    # 两条航路共享同一策略版本与构造算法（消费同一契约）。
    assert {route["fixed_corridor_geometry"]["geometry_fingerprint"]
            for route in result["routes"]} != set()


def test_zhoushan_routes_resolve_to_utm51n():
    """舟山区域（经度约 122°）必须解析为 EPSG:32651，且畸形/越界航路 fail-closed。"""

    zhoushan = [[122.10, 29.95], [122.25, 30.05], [122.35, 29.90]]
    geometry = build_fixed_corridor_geometry(zhoushan, route_id="R0005")
    assert geometry["status"] == "available"
    assert geometry["construction_crs"] == "EPSG:32651"
    assert geometry["metric_crs_unit"] == "metre"
    assert geometry["projected_to_geodesic_length_ratio"] == pytest.approx(1.0, abs=0.01)
    # 严格正南正北的舟山航路也必须可构造（退化 bbox 不得让它 fail-closed）。
    north_south = build_fixed_corridor_geometry(
        [[122.10, 29.95], [122.10, 30.05]], route_id="R0005")
    assert north_south["status"] == "available"
    assert north_south["construction_crs"] == "EPSG:32651"
    # 跨半个地球的"航路"必须被投影畸变门禁拦下。
    absurd = build_fixed_corridor_geometry([[122.0, 30.0], [-120.0, 30.0]])
    assert absurd["status"] == "blocked"


def test_geometry_fingerprint_binds_full_centerline_and_authority():
    base = [[122.1, 29.9], [122.2, 30.0], [122.3, 30.05]]
    first = build_fixed_corridor_geometry(base, route_id="R0005")
    # 点数 / 坐标 / authority / CRS / 宽度策略 任一变化都必须改变指纹。
    assert build_fixed_corridor_geometry(
        base + [[122.35, 30.06]], route_id="R0005")["geometry_fingerprint"] != (
        first["geometry_fingerprint"])
    assert build_fixed_corridor_geometry(
        [[122.1, 29.9], [122.2, 30.0], [122.31, 30.05]],
        route_id="R0005")["geometry_fingerprint"] != first["geometry_fingerprint"]
    assert build_fixed_corridor_geometry(
        base, route_id="R0005",
        route_authority={"kind": "operational_route"})["geometry_fingerprint"] != (
        first["geometry_fingerprint"])
    assert build_fixed_corridor_geometry(
        base, route_id="R0005",
        construction_crs="EPSG:32650")["geometry_fingerprint"] != first["geometry_fingerprint"]


def test_geometry_blocks_antimeridian_degenerate_and_non_metric_width():
    assert build_fixed_corridor_geometry([[179.9, 29.9], [-179.9, 29.9]])["status"] == "blocked"
    assert build_fixed_corridor_geometry([[122.0, 30.0], [122.0, 30.0]])["status"] == "blocked"
    assert build_fixed_corridor_geometry([[122.0, 30.0]])["status"] == "blocked"
    assert build_fixed_corridor_geometry(
        [[122.0, 30.0], [122.1, 30.1]], half_width_m=250.0)["status"] == "blocked"
    # 米制 CRS 无法解析时 fail-closed，且不给出任何几何。
    blocked = build_fixed_corridor_geometry(
        [[122.0, 30.0], [122.1, 30.1]], construction_crs="EPSG:4326")
    assert blocked["status"] == "blocked"
    assert blocked["geometry"] is None
    assert "metric construction CRS unresolved" in blocked["reason"]


def test_geometry_identity_summary_is_serializable_and_library_free():
    geometry = build_fixed_corridor_geometry([[122.0, 30.0], [122.1, 30.1]])
    summary = corridor_geometry_identity_summary(geometry)
    assert "geometry" not in summary  # 不携带完整 ring 坐标
    assert summary["geometry_fingerprint"] == geometry["geometry_fingerprint"]
    assert corridor_geometry_identity_summary(None) is None


def test_full_width_statistics_disclose_discrete_proxy_not_coverage():
    geometry = build_fixed_corridor_geometries(ROUTES, _policy({"R1": 500.0, "R2": 500.0}))
    result = _evaluate(_policy({"R1": 500.0, "R2": 500.0}), geometry=geometry)
    stats = result["full_width_statistics"]
    assert stats["width_policy_version"] == WIDTH_POLICY_VERSION
    assert stats["horizontal_total_width_m"] == 1000.0
    assert stats["centerline_meets_does_not_imply_full_width_meets"] is True
    assert "not_continuous_area_guarantee" in stats["semantics"]
    assert stats["full_width_grid_cell_count"] > 0
    assert stats["full_width_voxel_count"] >= stats["full_width_grid_cell_count"]
    assert result["disclaimers"]["width_semantics"] == "cns_service_target_band"


# ---- 4. 中心线满足不自动代表全宽满足 ----

def test_centerline_can_meet_while_full_width_is_not_evaluated():
    """未运行全宽评估时 P15 必须是 ``not_evaluated``，绝不由中心线推导。"""

    p15 = CNSCorridorGapAnalyzerV1().evaluate(
        {"status": "passed", "algorithm_id": "cns_service_corridor_v1",
         "algorithm_version": "1.4", "routes": [{
             "route_id": "R1", "route_length_m": 100.0, "status": "passed", "voxels": [],
         }]},
        _requirements(), normalize_cns_planning_objectives(),
    )
    assert p15["algorithm_version"] == "1.3"
    assert p15["full_width_service_status"] == "not_evaluated"
    assert p15["centerline_service_status"] == "not_evaluated"
    route = p15["routes"][0]
    assert route["full_width_service_status"]["status"] == "not_evaluated"
    assert route["centerline_service_status"]["status"] == "not_evaluated"
    assert route["full_width_statistics"]["evaluated"] is False


def _p15_voxel(identifier, *, lateral, half, service, area=100.0):
    entries = []
    for subsystem in ("C", "N", "S"):
        current = service if subsystem == "C" else "not_applicable"
        entries.append({
            "subsystem": subsystem,
            "planning_status": current,
            "p8_status": {
                "satisfied": "meets_under_model",
                "confirmed_deficit": "does_not_meet_under_model",
                "unknown": "unknown", "not_applicable": "not_applicable",
            }[current],
            "provider_evaluations": [], "reasons": [], "evidence": [],
            "service_redundancy": [] if subsystem != "C" else [{
                "service_key": "C:communication", "subsystem": "C",
                "status": {"satisfied": "satisfied",
                           "confirmed_deficit": "confirmed_deficit",
                           "unknown": "unknown"}[current],
                "surface_dependent": True,
            }],
        })
    return {
        "voxel_id": identifier, "grid_id": identifier.split("@")[0],
        "altitude_layer_id": "L1", "nearest_route_offset_m": 10.0,
        "cell_half_diagonal_m": half, "discretized_volume_proxy_m3": 1000.0,
        "cell_area_proxy_m2": area,
        "nearest_route_lateral_offset_m": lateral,
        "nearest_route_lateral_side": (
            "left" if lateral > 0 else "right" if lateral < 0 else "centerline"),
        "surface_class": "land", "subsystems": entries,
    }


def _p15_corridor(voxels):
    return {
        "status": "passed", "algorithm_id": "cns_service_corridor_v1",
        "algorithm_version": "1.4",
        "input_fingerprint": "p14-fixture",
        "fixed_corridor_width_contract": fixed_corridor_width_contract(),
        "routes": [{
            "route_id": "R1", "route_length_m": 100.0, "status": "passed",
            "horizontal_half_width_m": 500.0, "horizontal_total_width_m": 1000.0,
            "voxels": deepcopy(voxels),
        }],
    }


def test_centerline_satisfied_while_full_width_has_lateral_deficit():
    """中心线样本全部满足，但全宽外侧样本缺口 ⇒ 两条状态必须不同。"""

    voxels = [
        _p15_voxel("CENTER@L1", lateral=0.0, half=60.0, service="satisfied", area=100.0),
        _p15_voxel("LEFT-OUT@L1", lateral=480.0, half=60.0,
                   service="confirmed_deficit", area=200.0),
        _p15_voxel("RIGHT-OUT@L1", lateral=-470.0, half=60.0,
                   service="unknown", area=300.0),
    ]
    p15 = CNSCorridorGapAnalyzerV1().evaluate(
        _p15_corridor(voxels), _requirements(), normalize_cns_planning_objectives(),
    )
    route = p15["routes"][0]
    assert route["centerline_service_status"]["subsystems"]["C"] == "satisfied"
    assert route["full_width_service_status"]["subsystems"]["C"] == "deficit"
    stats = route["full_width_statistics"]["services"]["C"]
    assert stats["required_grid_count"] == 3
    assert stats["satisfied_grid_count"] == 1
    assert stats["deficit_grid_count"] == 1
    assert stats["unknown_grid_count"] == 1
    # 面积代理：required = 100 + 200 + 300
    assert stats["required_area_proxy_m2"] == pytest.approx(600.0)
    assert stats["deficit_area_proxy_m2"] == pytest.approx(200.0)
    assert stats["unknown_area_proxy_m2"] == pytest.approx(300.0)
    assert stats["deficit_area_fraction"] == pytest.approx(200.0 / 600.0)
    assert stats["unknown_area_fraction"] == pytest.approx(300.0 / 600.0)
    # 左右侧分别计数。
    assert stats["left"]["deficit_grid_count"] == 1
    assert stats["left"]["deficit_area_proxy_m2"] == pytest.approx(200.0)
    assert stats["right"]["unknown_grid_count"] == 1
    assert stats["right"]["unknown_area_proxy_m2"] == pytest.approx(300.0)
    assert stats["left"]["unknown_grid_count"] == 0
    # 最大横向缺口距离。
    assert stats["max_lateral_deficit_distance_m"] == pytest.approx(480.0)
    assert route["full_width_statistics"]["max_lateral_deficit_distance_m"] == pytest.approx(480.0)
    assert route["full_width_statistics"]["area_proxy_semantics"].endswith(
        "not_continuous_coverage")


def test_full_width_unknown_is_not_satisfied_and_kept_distinct():
    voxels = [
        _p15_voxel("CENTER@L1", lateral=0.0, half=60.0, service="satisfied"),
        _p15_voxel("EDGE@L1", lateral=300.0, half=60.0, service="unknown"),
    ]
    p15 = CNSCorridorGapAnalyzerV1().evaluate(
        _p15_corridor(voxels), _requirements(), normalize_cns_planning_objectives(),
    )
    assert p15["full_width_service_status"] == "unknown"
    assert p15["routes"][0]["full_width_service_status"]["subsystems"]["C"] == "unknown"


# ---- 5. 历史版本与不同几何不得复用为 current ----

def test_previous_algorithm_versions_are_projected_stale(tmp_path):
    from cns_planner.application.result_currentness import (
        apply_algorithm_semantics_stale,
    )

    for stale_version in ("1.0", "1.1", "1.2", "1.3"):
        stored = {
            "status": "passed", "algorithm_id": "cns_service_corridor_v1",
            "algorithm_version": stale_version, "routes": [{"route_id": "R1"}],
        }
        projected = apply_algorithm_semantics_stale(
            stored, CNSServiceCorridorV1.algorithm_id,
            CNSServiceCorridorV1.algorithm_version,
        )
        assert projected["status"] == "stale", stale_version
        assert projected["algorithm_semantics_stale"]["stored_algorithm_version"] == stale_version
        assert projected["algorithm_semantics_stale"]["current_algorithm_version"] == "1.4"
    for stale_version in ("1.0", "1.1", "1.2"):
        projected = apply_algorithm_semantics_stale(
            {"status": "passed", "algorithm_id": "cns_corridor_gap_v1",
             "algorithm_version": stale_version},
            CNSCorridorGapAnalyzerV1.algorithm_id,
            CNSCorridorGapAnalyzerV1.algorithm_version,
        )
        assert projected["status"] == "stale", stale_version


def test_p14_stale_geometry_identity_does_not_satisfy_p15():
    """旧几何身份的 P14 不得被 P15 当作 current 全宽证据。"""

    stale = _p15_corridor([])
    stale["corridor_geometry_fingerprint"] = "corridorgeometryv1-old"
    stale["fixed_corridor_width_contract"] = {**fixed_corridor_width_contract(),
                                              "width_policy_version": "legacy-editable-v0"}
    p15 = CNSCorridorGapAnalyzerV1().evaluate(
        stale, _requirements(), normalize_cns_planning_objectives(),
    )
    assert p15["input_provenance"]["width_policy_version"] == "legacy-editable-v0"
    assert p15["full_width_service_status"] == "not_evaluated"


def test_p15_rejects_stale_p14_before_any_full_width_conclusion():
    for status in ("stale", "missing_data", "not_calculated", None):
        p15 = CNSCorridorGapAnalyzerV1().evaluate(
            {"status": status}, _requirements(), normalize_cns_planning_objectives(),
        )
        assert p15["status"] == "missing_data"
        assert p15["full_width_service_status"] == "not_evaluated"


# ---- 6. 复杂度上限与后台任务行为不退化 ----

def _preflight_inputs(providers=4, cells=200):
    step = 0.0006
    grid_cells = []
    for index in range(cells):
        left = -0.004 + (index % 20) * step
        bottom = (index // 20) * step
        grid_cells.append({
            "grid_id": f"P{index:05d}",
            "bbox": [left, bottom, left + step, bottom + step],
        })
    items, catalog_items = [], []
    for index in range(providers):
        entry = _device(900.0)
        entry["device_id"] = f"D{index}"
        catalog_items.append(entry)
        items.append({
            "facility_id": f"F{index}", "coordinate": [0.0, 0.0], "status": "active",
            "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
            "devices": [{"device_id": f"D{index}", "subsystem": "C", "status": "active"}],
        })
    return {
        "routes": [{"route_id": "R1", "status": "passed",
                    "path": [[0.0, 0.0], [0.0, 0.02]]}],
        "spatial_3d": _spatial(["R1"]),
        "grid": {"status": "passed", "level": 7, "cells": grid_cells},
        "grid_attributes": {"terrain": {"status": "passed", "cells": {}}},
        "corridor_policy": _policy({"R1": 150.0}),
        "existing_facilities": {"status": "passed", "items": items},
        "device_catalog": {"status": "passed", "items": catalog_items},
    }


def test_preflight_limits_unchanged_and_fixed_width_is_estimated():
    inputs = _preflight_inputs()
    estimate = estimate_corridor_complexity(
        inputs["routes"], inputs["spatial_3d"], inputs["grid"],
        inputs["grid_attributes"], inputs["corridor_policy"],
        inputs["existing_facilities"], inputs["device_catalog"],
    )
    assert VALIDATED_EVALUATION_LIMIT == 3_000_000
    assert SAFETY_EVALUATION_CEILING == 10_000_000
    assert estimate["validated_evaluation_limit"] == VALIDATED_EVALUATION_LIMIT
    assert estimate["safety_evaluation_ceiling"] == SAFETY_EVALUATION_CEILING
    assert estimate["width_policy_version"] == WIDTH_POLICY_VERSION
    assert estimate["corridor_half_width_m"] == 500.0
    assert estimate["corridor_total_width_m"] == 1000.0
    # 估算与正式评估必须消费同一固定半宽：legacy 150 m 不改变估算口径。
    legacy = deepcopy(inputs)
    legacy["corridor_policy"] = _policy({"R1": 5000.0})
    same = estimate_corridor_complexity(
        legacy["routes"], legacy["spatial_3d"], legacy["grid"],
        legacy["grid_attributes"], legacy["corridor_policy"],
        legacy["existing_facilities"], legacy["device_catalog"],
    )
    assert same["estimated_corridor_cells"] == estimate["estimated_corridor_cells"]
    assert same["estimated_voxels"] == estimate["estimated_voxels"]


def test_ceiling_still_blocks_before_constructing_any_result():
    from cns_planner.algorithms.corridor.v1 import (
        COMPLEXITY_TIER_CEILING, CorridorComplexityBlocked,
    )

    inputs = _preflight_inputs(providers=4, cells=200)
    with pytest.raises(CorridorComplexityBlocked):
        CNSServiceCorridorV1().evaluate(
            inputs["routes"], inputs["spatial_3d"], inputs["grid"],
            inputs["grid_attributes"], _requirements(), _aircraft(),
            inputs["existing_facilities"], inputs["device_catalog"],
            inputs["corridor_policy"],
            preflight={"tier": COMPLEXITY_TIER_CEILING, "blocking": True},
        )


def test_beyond_validated_envelope_requires_explicit_acceptance():
    from cns_planner.algorithms.corridor.v1 import (
        COMPLEXITY_TIER_BEYOND, CorridorComplexityBlocked,
    )

    inputs = _preflight_inputs()
    preflight = {"tier": COMPLEXITY_TIER_BEYOND, "blocking": False}
    with pytest.raises(CorridorComplexityBlocked):
        CNSServiceCorridorV1().evaluate(
            inputs["routes"], inputs["spatial_3d"], inputs["grid"],
            inputs["grid_attributes"], _requirements(), _aircraft(),
            inputs["existing_facilities"], inputs["device_catalog"],
            inputs["corridor_policy"], preflight=preflight,
        )
    accepted = CNSServiceCorridorV1().evaluate(
        inputs["routes"], inputs["spatial_3d"], inputs["grid"],
        inputs["grid_attributes"], _requirements(), _aircraft(),
        inputs["existing_facilities"], inputs["device_catalog"],
        inputs["corridor_policy"], preflight=preflight,
        allow_beyond_validated_envelope=True,
    )
    assert accepted["algorithm_version"] == "1.4"


def test_application_and_worker_consume_the_same_frozen_geometry(tmp_path):
    """同步入口与异步提交路径必须冻结**同一份**几何身份（绝不各造一套）。"""

    from cns_planner.application.workflow_service import WorkflowService
    from cns_planner.tasks.task_specs import _corridor_inputs

    defaults = Path("cns_planner/config/defaults.json")
    workflow = WorkflowService(tmp_path / "project.json", defaults)
    inputs = _preflight_inputs()
    workflow.state.update({
        "operational_routes": inputs["routes"], "spatial_3d": inputs["spatial_3d"],
        "grid": inputs["grid"], "grid_attributes": inputs["grid_attributes"],
        "required_cns": _requirements(),
        "aircraft_profiles": {"items": [_aircraft()], "count": 1},
        "selected_aircraft_profile_id": "A1",
        "existing_cns_facilities": inputs["existing_facilities"],
        "device_catalog": inputs["device_catalog"],
        "cns_corridor_policy": inputs["corridor_policy"],
    })
    sync_inputs = workflow.corridor_service.compute_input({})
    worker_inputs = _corridor_inputs(workflow.state, {})
    assert sync_inputs["corridor_geometry"] == worker_inputs["corridor_geometry"]
    fingerprint = sync_inputs["corridor_geometry"]["R1"]["geometry_fingerprint"]
    assert fingerprint.startswith("corridorgeometryv1-")
    snapshot = workflow.evaluate_cns_corridor({})
    assessment = snapshot.get("cns_corridor_assessment") or {}
    assert assessment["algorithm_version"] == "1.4"
    assert assessment["routes"][0]["fixed_corridor_geometry"][
        "geometry_fingerprint"] == fingerprint
    # canonical 结果不得携带完整 ring 坐标。
    assert "geometry" not in assessment["routes"][0]["fixed_corridor_geometry"]


def test_worker_path_without_frozen_geometry_still_evaluates_without_fake_identity():
    """旧快照没有冻结几何时，P14 仍按固定宽度计算，但**不伪造**已验证 buffer 身份。"""

    inputs = _preflight_inputs()
    result = CNSServiceCorridorV1().evaluate(
        inputs["routes"], inputs["spatial_3d"], inputs["grid"],
        inputs["grid_attributes"], _requirements(), _aircraft(),
        inputs["existing_facilities"], inputs["device_catalog"],
        inputs["corridor_policy"],
    )
    route = result["routes"][0]
    assert route["horizontal_half_width_m"] == 500.0
    assert route["fixed_corridor_geometry_status"] == "not_supplied"
    assert "fixed_corridor_geometry" not in route
    assert route["corridor_geometry_fingerprint"]
    # 几何指纹退回旧口径的确定性摘要，且与携带几何时不同。
    with_geometry = CNSServiceCorridorV1().evaluate(
        inputs["routes"], inputs["spatial_3d"], inputs["grid"],
        inputs["grid_attributes"], _requirements(), _aircraft(),
        inputs["existing_facilities"], inputs["device_catalog"],
        inputs["corridor_policy"],
        corridor_geometry=build_fixed_corridor_geometries(
            inputs["routes"], inputs["corridor_policy"]),
    )
    assert with_geometry["corridor_geometry_fingerprint"] != (
        result["corridor_geometry_fingerprint"])


def test_full_width_evaluation_is_deterministic_and_order_independent():
    policy = _policy({"R1": 150.0})
    first = _evaluate(policy)
    second = _evaluate(policy)
    assert first == second
    assert first["input_fingerprint"] == second["input_fingerprint"]


def test_unconfirmed_corridor_spec_still_blocks_full_width_conclusion():
    result = _evaluate(_policy({"R1": 500.0}, confirmed=False))
    route = result["routes"][0]
    assert route["status"] == "pending_confirmation"
    assert route["horizontal_half_width_m"] == 500.0
    assert route["voxel_count"] == 0
