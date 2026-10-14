"""Round38-W1A 证据生成：固定宽带几何样例、P14 1.4 与 P15 1.3 指标样例。

只读、确定性：不读也不写任何 ProjectState，不调用任何失效入口；全部输入都是本文件
内显式构造的工程夹具，用于可核查地展示 W1A 的四项交付内容。

用法（仓库根目录）：
    D:\\tools\\anaconda\\python.exe tools\\round38_w1a_evidence.py <输出目录>
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cns_planner.algorithms.corridor.v1 import (  # noqa: E402
    CNSServiceCorridorV1, estimate_corridor_complexity,
)
from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1  # noqa: E402
from cns_planner.domain.cns_corridor import (  # noqa: E402
    fixed_corridor_width_contract, normalize_cns_corridor_policy,
)
from cns_planner.domain.cns_planning_objectives import (  # noqa: E402
    normalize_cns_planning_objectives,
)
from cns_planner.gis.corridor_geometry import (  # noqa: E402
    build_fixed_corridor_geometry, build_fixed_corridor_geometries,
)


#: 两条**真实感**的舟山航路中心线（经度 122° 附近 ⇒ EPSG:32651）。
ROUTES = [
    {
        "route_id": "R0005-LIKE",
        "status": "passed",
        "path": [
            [122.1800, 29.9200], [122.2000, 29.9450], [122.2150, 29.9700],
            [122.2400, 29.9900], [122.2600, 30.0150],
        ],
        "authority": "canonical_operational_route",
    },
    {
        "route_id": "R0006-LIKE",
        "status": "passed",
        "path": [
            [122.1900, 29.9350], [122.2100, 29.9600], [122.2300, 29.9800],
        ],
        "authority": "canonical_operational_route",
    },
]


def _spatial():
    return {
        "status": "passed",
        "altitude_layers": [{
            "altitude_layer_id": "ALT-100", "name": "ALT-100",
            "lower_altitude_m": 90.0, "upper_altitude_m": 120.0,
            "vertical_reference": "egm2008_orthometric",
            "source": "engineering_baseline", "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {
            route["route_id"]: {
                "route_id": route["route_id"], "mode": "constant",
                "vertical_reference": "egm2008_orthometric",
                "constant_altitude_m": 100.0, "waypoints": [],
                "source": "engineering_baseline", "confirmed": True,
                "status": "confirmed", "geoid_undulation_m": None,
            } for route in ROUTES
        },
        "site_vertical_profiles": {},
    }


def _grid(step=0.005, columns=20, rows=20):
    cells = []
    for row in range(rows):
        for column in range(columns):
            left = 122.1700 + column * step
            bottom = 29.9100 + row * step
            cells.append({
                "grid_id": f"L8-{row:03d}{column:03d}",
                "bbox": [left, bottom, left + step, bottom + step],
            })
    return {"status": "passed", "level": 8, "cells": cells}


def _device(radius_m=2000.0):
    """与 P14 正式输入同构的设备（经 ``normalize_device`` 归一化，绝不手搓字段）。"""

    from cns_planner.domain.cns_inputs import normalize_device

    return normalize_device({
        "device_id": "D-C-1", "name": "D-C-1", "subsystem": "C", "role": "existing",
        "radius_m": radius_m, "mtbf_h": 1000,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "sphere", "slant_range_m": radius_m,
            "source": "engineering_baseline", "confirmed": True,
        },
        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "dedicated_radio",
            "parameters": {"performance": {"max_latency_s": 0.2}},
            "source": "engineering_baseline", "confirmed": True,
        },
    })


def _facilities(radius_m=2000.0, coordinate=None, service_origin_m=60.0):
    """已有 CNS 站点；安装条目携带**与设备目录一致**的 canonical 设备事实。

    ``build_provider_devices`` 会用站点安装条目里的 ``service_model`` 覆盖目录条目
    （既有权威行为），因此安装记录必须与目录一致 —— 这也是真实项目的正常形态。
    """

    device = _device(radius_m)
    installed = {
        "device_id": device["device_id"], "subsystem": device["subsystem"],
        "status": "active",
        "type": deepcopy(device["type"]),
        "performance": deepcopy(device["performance"]),
        "coverage_geometry": deepcopy(device["coverage_geometry"]),
        "service_model": deepcopy(device["service_model"]),
    }
    return {"status": "passed", "items": [{
        "facility_id": "F-EXISTING-1",
        "coordinate": list(coordinate or [122.1940, 29.9400]),
        "status": "active",
        "vertical_profile": {
            "service_origin_egm2008_m": service_origin_m, "confirmed": True,
        },
        "devices": [installed],
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


def _policy(widths, confirmed=True):
    return normalize_cns_corridor_policy({"routes": {
        route["route_id"]: {
            "route_id": route["route_id"], "horizontal_half_width_m": widths[route["route_id"]],
            "vertical_lower_margin_m": 10.0, "vertical_upper_margin_m": 10.0,
            "source": "engineering_baseline", "confirmed": confirmed,
        } for route in ROUTES
    }})


def _geometry_sample():
    """几何样例：固定 1000 m 契约、左右各 500 m 的连续米制 buffer。"""

    samples = []
    for route in ROUTES:
        geometry = build_fixed_corridor_geometry(
            route["path"], route_id=route["route_id"],
            route_authority=route["authority"],
        )
        samples.append({
            "route_id": route["route_id"],
            "centerline_point_count": geometry["point_count"],
            "centerline_first_point": route["path"][0],
            "centerline_last_point": route["path"][-1],
            "status": geometry["status"],
            "width_policy_version": geometry["width_policy_version"],
            "corridor_half_width_m": geometry["corridor_half_width_m"],
            "corridor_total_width_m": geometry["corridor_total_width_m"],
            "cap_style": geometry["cap_style"], "join_style": geometry["join_style"],
            "quad_segs": geometry["quad_segs"],
            "source_crs": geometry["source_crs"],
            "construction_crs": geometry["construction_crs"],
            "metric_crs_source": geometry["metric_crs_source"],
            "route_length_projected_m": round(geometry["route_length_projected_m"], 3),
            "route_length_geodesic_m": round(geometry["route_length_geodesic_m"], 3),
            "projected_to_geodesic_length_ratio": round(
                geometry["projected_to_geodesic_length_ratio"], 6),
            "projected_area_m2": round(geometry["projected_area_m2"], 1),
            "analytic_area_m2": round(
                2 * 500.0 * geometry["route_length_projected_m"] + 3.141592653589793 * 500.0 ** 2, 1),
            "boundary_ring_count": geometry["boundary_ring_count"],
            "is_valid": geometry["is_valid"],
            "geometry_fingerprint": geometry["geometry_fingerprint"],
            "fingerprint_inputs": geometry["fingerprint_inputs"],
        })
    # 几何指纹必须对"完整中心线 / authority / CRS / 宽度策略"敏感。
    base = build_fixed_corridor_geometry(ROUTES[0]["path"], route_id=ROUTES[0]["route_id"])
    sensitivity = {
        "baseline": base["geometry_fingerprint"],
        "point_count_changed": build_fixed_corridor_geometry(
            ROUTES[0]["path"] + [[122.2700, 30.0300]],
            route_id=ROUTES[0]["route_id"])["geometry_fingerprint"],
        "coordinate_changed": build_fixed_corridor_geometry(
            ROUTES[0]["path"][:-1] + [[122.2601, 30.0151]],
            route_id=ROUTES[0]["route_id"])["geometry_fingerprint"],
        "route_authority_changed": build_fixed_corridor_geometry(
            ROUTES[0]["path"], route_id=ROUTES[0]["route_id"],
            route_authority="engineering_demo_projection")["geometry_fingerprint"],
        "route_id_changed": build_fixed_corridor_geometry(
            ROUTES[0]["path"], route_id="R0006-LIKE")["geometry_fingerprint"],
        #: 换一个**在该区域同样有效**的米制 CRS（Web Mercator）⇒ 构造成功但身份不同。
        "construction_crs_changed_to_3857": build_fixed_corridor_geometry(
            ROUTES[0]["path"], route_id=ROUTES[0]["route_id"],
            construction_crs="EPSG:3857")["geometry_fingerprint"],
    }
    blocked = {
        label: build_fixed_corridor_geometry(path, route_id="X")["reason"]
        for label, path in (
            ("antimeridian", [[179.9, 29.9], [-179.9, 29.9]]),
            ("duplicate_points", [[122.0, 30.0], [122.0, 30.0]]),
            ("single_point", [[122.0, 30.0]]),
        )
    }
    blocked["non_metric_explicit_crs"] = build_fixed_corridor_geometry(
        [[122.0, 30.0], [122.1, 30.1]], construction_crs="EPSG:4326")["reason"]
    #: 显式给出的米制 CRS 若其适用区不含航路，必须 fail-closed（而不是静默换一个）。
    blocked["construction_crs_area_of_use_mismatch"] = build_fixed_corridor_geometry(
        ROUTES[0]["path"], route_id=ROUTES[0]["route_id"],
        construction_crs="EPSG:32650")["reason"]
    return {
        "schema": "round38-w1a-fixed-corridor-geometry-sample/1",
        "contract": fixed_corridor_width_contract(),
        "samples": samples,
        "fingerprint_sensitivity": sensitivity,
        "blocked_cases": blocked,
        "notes": [
            "宽度只由固定契约 fixed-1000m-v1 决定，禁止用经纬度差近似宽度。",
            "construction CRS 由航路地理范围解析（此处 EPSG:32651），解析失败一律 fail-closed。",
            "cap/join 均为 round，quad_segs 只影响圆弧离散，不影响 (x,y) 与 buffer 距离。",
            "几何身份绑定完整中心线坐标、点数、route authority、source/construction CRS、"
            "宽度策略版本、cap/join 与构造算法版本。",
        ],
    }


#: 指标样例使用的**确定性演示航路**：东西向直线，长约 890 m（0.008° 经度）。
DEMO_ROUTE = {
    "route_id": "R-DEMO",
    "status": "passed",
    "path": [[122.1900, 29.9400], [122.1980, 29.9400]],
    "authority": "canonical_operational_route",
}

#: 演示网格：0.0008° ≈ 77 m 网格，覆盖中心线两侧约 ±560 m（含 500 m 契约边界外一格）。
DEMO_STEP = 0.0008
DEMO_COLUMNS = 18
DEMO_ROWS = 16


def _demo_grid():
    cells = []
    for row in range(DEMO_ROWS):
        for column in range(DEMO_COLUMNS):
            left = 122.1870 + column * DEMO_STEP
            bottom = 29.9350 + row * DEMO_STEP
            cells.append({
                "grid_id": f"L9-{row:03d}{column:03d}",
                "bbox": [left, bottom, left + DEMO_STEP, bottom + DEMO_STEP],
            })
    return {"status": "passed", "level": 9, "cells": cells}


def _demo_spatial():
    return {
        "status": "passed",
        "altitude_layers": [{
            "altitude_layer_id": "ALT-100", "name": "ALT-100",
            "lower_altitude_m": 90.0, "upper_altitude_m": 120.0,
            "vertical_reference": "egm2008_orthometric",
            "source": "engineering_baseline", "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {DEMO_ROUTE["route_id"]: {
            "route_id": DEMO_ROUTE["route_id"], "mode": "constant",
            "vertical_reference": "egm2008_orthometric",
            "constant_altitude_m": 100.0, "waypoints": [],
            "source": "engineering_baseline", "confirmed": True,
            "status": "confirmed", "geoid_undulation_m": None,
        }},
        "site_vertical_profiles": {},
    }


def _demo_policy(width_m=500.0):
    return normalize_cns_corridor_policy({"routes": {DEMO_ROUTE["route_id"]: {
        "route_id": DEMO_ROUTE["route_id"],
        "horizontal_half_width_m": width_m,
        "vertical_lower_margin_m": 10.0, "vertical_upper_margin_m": 10.0,
        "source": "engineering_baseline", "confirmed": True,
    }}})


#: 演示 provider：位于航路中点正下方偏左，服务半径 900 m。于是中心线样本全部满足，
#: 而靠近 ±500 m 契约边界的外侧网格超出服务半径 ⇒ 全宽在**左右两侧**都出现已确认缺口。
DEMO_RADIUS_M = 900.0
DEMO_SITE = [122.1925, 29.9400]


def _demo_facilities():
    return _facilities(
        radius_m=DEMO_RADIUS_M, coordinate=DEMO_SITE, service_origin_m=100.0,
    )


def _p14_p15_sample():
    """P14 1.4 与 P15 1.3 指标样例：中心线满足但全宽存在缺口。"""

    policy = _demo_policy()
    geometry = build_fixed_corridor_geometries([DEMO_ROUTE], policy)
    grid = _demo_grid()
    spatial = _demo_spatial()
    facilities = _demo_facilities()
    catalog = {"status": "passed", "items": [_device(DEMO_RADIUS_M)]}
    p14 = CNSServiceCorridorV1().evaluate(
        [DEMO_ROUTE], spatial, grid, {"terrain": {"status": "passed", "cells": {}}},
        _requirements(), _aircraft(), facilities, catalog,
        policy, corridor_geometry=geometry,
    )
    p15 = CNSCorridorGapAnalyzerV1().evaluate(
        p14, _requirements(), normalize_cns_planning_objectives(),
    )
    estimate = estimate_corridor_complexity(
        [DEMO_ROUTE], spatial, grid, {"terrain": {"status": "passed", "cells": {}}},
        policy, facilities, catalog,
    )
    # 旧半宽对照：同一输入、只把 legacy 半宽改成 150 m ⇒ 结论与规模估算必须逐字段不变。
    legacy_policy = _demo_policy(150.0)
    legacy_p14 = CNSServiceCorridorV1().evaluate(
        [DEMO_ROUTE], spatial, grid, {"terrain": {"status": "passed", "cells": {}}},
        _requirements(), _aircraft(), facilities, catalog,
        legacy_policy, corridor_geometry=build_fixed_corridor_geometries(
            [DEMO_ROUTE], legacy_policy),
    )
    legacy_estimate = estimate_corridor_complexity(
        [DEMO_ROUTE], spatial, grid, {"terrain": {"status": "passed", "cells": {}}},
        legacy_policy, facilities, catalog,
    )
    routes = []
    for corridor_route, gap_route in zip(p14["routes"], p15["routes"]):
        stats = gap_route["full_width_statistics"]
        communication = stats["services"]["C"]
        routes.append({
            "route_id": gap_route["route_id"],
            "p14_status": corridor_route["status"],
            "horizontal_half_width_m": corridor_route["horizontal_half_width_m"],
            "horizontal_total_width_m": corridor_route["horizontal_total_width_m"],
            "legacy_horizontal_half_width_m": corridor_route["legacy_horizontal_half_width_m"],
            "legacy_horizontal_half_width_status": (
                corridor_route["legacy_horizontal_half_width_status"]),
            "horizontal_cell_count": corridor_route["horizontal_cell_count"],
            "voxel_count": corridor_route["voxel_count"],
            "fixed_corridor_geometry_fingerprint": (
                (corridor_route.get("fixed_corridor_geometry") or {}).get("geometry_fingerprint")),
            "discrete_corridor_geometry_fingerprint": corridor_route[
                "corridor_geometry_fingerprint"],
            "p15_centerline_service_status": gap_route["centerline_service_status"]["status"],
            "p15_full_width_service_status": gap_route["full_width_service_status"]["status"],
            "p15_centerline_subsystem_status": gap_route[
                "centerline_service_status"]["subsystems"],
            "p15_full_width_subsystem_status": gap_route[
                "full_width_service_status"]["subsystems"],
            "full_width_evaluated": stats["evaluated"],
            "communication_service": {
                key: communication[key] for key in (
                    "required_grid_count", "satisfied_grid_count", "deficit_grid_count",
                    "unknown_grid_count", "not_applicable_grid_count",
                    "required_area_proxy_m2", "satisfied_area_proxy_m2",
                    "deficit_area_proxy_m2", "unknown_area_proxy_m2",
                    "deficit_area_fraction", "unknown_area_fraction",
                    "max_lateral_deficit_distance_m", "status",
                )
            },
            "communication_left": communication["left"],
            "communication_right": communication["right"],
        })
    return {
        "schema": "round38-w1a-p14-p15-metrics-sample/1",
        "fixture": {
            "route_path": DEMO_ROUTE["path"],
            "route_authority": DEMO_ROUTE["authority"],
            "grid_level": grid["level"], "grid_cell_count": len(grid["cells"]),
            "grid_step_deg": DEMO_STEP,
            "provider_site_coordinate": DEMO_SITE,
            "provider_slant_range_m": DEMO_RADIUS_M,
            "altitude_layer": "ALT-100 (90–120 m, EGM2008 orthometric)",
            "note": "确定性工程夹具，用于展示中心线口径与全宽口径的差异；不是正式项目结论。",
        },
        "p14": {
            "algorithm_id": p14["algorithm_id"], "algorithm_version": p14["algorithm_version"],
            "status": p14["status"],
            "fixed_corridor_width_contract": p14["fixed_corridor_width_contract"],
            "full_width_statistics": {
                key: p14["full_width_statistics"][key] for key in (
                    "width_policy_version", "horizontal_half_width_m",
                    "horizontal_total_width_m", "route_count",
                    "full_width_grid_cell_count", "full_width_voxel_count",
                    "semantics", "centerline_meets_does_not_imply_full_width_meets",
                )
            },
            "input_fingerprint": p14["input_fingerprint"],
            "corridor_geometry_fingerprint": p14["corridor_geometry_fingerprint"],
        },
        "p15": {
            "algorithm_id": p15["algorithm_id"], "algorithm_version": p15["algorithm_version"],
            "status": p15["status"],
            "centerline_service_status": p15["centerline_service_status"],
            "full_width_service_status": p15["full_width_service_status"],
            "full_width_semantics": p15["full_width_semantics"],
            "input_provenance": p15["input_provenance"],
        },
        "routes": routes,
        "complexity_estimate": estimate,
        "legacy_width_equivalence": {
            "legacy_half_width_m": 150.0,
            "fixed_half_width_m": 500.0,
            "same_horizontal_cell_count": (
                legacy_p14["routes"][0]["horizontal_cell_count"]
                == p14["routes"][0]["horizontal_cell_count"]
            ),
            "same_voxel_ids": (
                [v["voxel_id"] for v in legacy_p14["routes"][0]["voxels"]]
                == [v["voxel_id"] for v in p14["routes"][0]["voxels"]]
            ),
            "same_subsystem_statuses": (
                legacy_p14["routes"][0]["subsystems"] == p14["routes"][0]["subsystems"]
            ),
            "same_estimated_corridor_cells": (
                legacy_estimate["estimated_corridor_cells"]
                == estimate["estimated_corridor_cells"]
            ),
            "same_estimated_voxels": (
                legacy_estimate["estimated_voxels"] == estimate["estimated_voxels"]
            ),
            "same_estimated_evaluation_upper_bound": (
                legacy_estimate["estimated_evaluation_upper_bound"]
                == estimate["estimated_evaluation_upper_bound"]
            ),
            "input_fingerprint_differs": (
                legacy_p14["input_fingerprint"] != p14["input_fingerprint"]
            ),
            "legacy_status": legacy_p14["routes"][0][
                "legacy_horizontal_half_width_status"],
        },
        "not_evaluated_check": _not_evaluated_check(),
        "notes": [
            "P14 消费唯一固定半宽 500 m；legacy 非 500 m 只读标记 stale，不参与包含判定。",
            "P15 的 centerline_service_status 与 full_width_service_status 是两个独立字段；"
            "中心线满足不推出全宽满足。",
            "全宽统计使用网格数与面积代理，不是精确连续空间覆盖率。",
            "未运行全宽评估时固定 not_evaluated，绝不由 N1 的 2000 m 关联结果推导。",
        ],
    }


def _not_evaluated_check():
    """证明"未评估全宽"与"中心线达标"被严格区分。"""

    empty = CNSCorridorGapAnalyzerV1().evaluate(
        {"status": "passed", "algorithm_id": "cns_service_corridor_v1",
         "algorithm_version": "1.4",
         "fixed_corridor_width_contract": fixed_corridor_width_contract(),
         "routes": [{"route_id": "R-EMPTY", "route_length_m": 0.0,
                     "status": "passed", "voxels": []}]},
        _requirements(), normalize_cns_planning_objectives(),
    )
    legacy = CNSCorridorGapAnalyzerV1().evaluate(
        {"status": "passed", "algorithm_id": "cns_service_corridor_v1",
         "algorithm_version": "1.4",
         "fixed_corridor_width_contract": {
             **fixed_corridor_width_contract(), "width_policy_version": "legacy-editable-v0"},
         "routes": [{"route_id": "R-LEGACY", "route_length_m": 100.0,
                     "status": "passed", "voxels": [{"voxel_id": "V1", "subsystems": []}]}]},
        _requirements(), normalize_cns_planning_objectives(),
    )
    return {
        "no_voxels": {
            "full_width_service_status": empty["full_width_service_status"],
            "centerline_service_status": empty["centerline_service_status"],
            "evaluated": empty["routes"][0]["full_width_statistics"]["evaluated"],
        },
        "legacy_width_policy_upstream": {
            "full_width_service_status": legacy["full_width_service_status"],
            "evaluated": legacy["routes"][0]["full_width_statistics"]["evaluated"],
            "width_policy_conflict": legacy["routes"][0][
                "full_width_statistics"]["width_policy_conflict"],
        },
    }


def main():
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        REPO_ROOT / "outputs" / "round38_w1a")
    output.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("geometry_sample.json", _geometry_sample()),
        ("p14_p15_metrics_sample.json", _p14_p15_sample()),
    ):
        (output / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(f"wrote {output / name}")


if __name__ == "__main__":
    main()
