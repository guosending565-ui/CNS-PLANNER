"""Round34-T07-A —— 真实数据的**只读**逐塔物理输入与地形 LOS 小样本验证。

必须用带 GDAL 的解释器运行（本机 = QGIS Python）::

    "C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat" `
        tools\\round34_t07a_real_data_terrain_los.py `
        --towers <towers.json> --profiles <tower_obstacle_profiles.json> `
        --policy <radar_surveillance_policy.json> `
        --terrain "D:\\...\\Zhoushan_FABDEM_DTM_30m.tif" `
        --out "D:\\Projects\\_cns_experiments\\Round34\\Round34-T07-A"

它做四件事（全部只读，不写 ``ProjectState``、不改源数据）：

1. 用既有 :class:`NativeTerrainWindowSource`（同一份已验证的 FABDEM 只读访问器）对
   373 座塔坐标采样 EGM2008 正高；
2. 用 :func:`cns_planner.gis.radar_layout_adapter.resolve_radar_origins` 的**同一套**
   分层原点解析（设备声明 → 已解析塔顶 → 策略授权的源表估算 → unknown）产出原点，
   再用 T07-A 的物理输入诊断标记"原点不高于地表"的塔；
3. 对抽样塔 × 人工构造的 probe（**由项目高度层目录的 nominal_altitude_m 给出高度**，
   绝不用走廊代表高度）跑地形 LOS，统计 clear / blocked / unknown 与净空分布；
4. 逐 probe 重复评估验证确定性，并用 :class:`TerrainSourceCache` 报告"每路径只打开
   一次"的缓存统计。

本脚本**不**运行 P14/P15/P16 规划，**不**写任何正式 artifact。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cns_planner.domain.altitude_layer_defaults import (  # noqa: E402
    default_altitude_layers,
)
from cns_planner.domain.radar_surveillance_layout import (  # noqa: E402
    ELEVATION_CENTER_DEG, ELEVATION_MAX_DEG, ELEVATION_MIN_DEG,
    RADAR_GEOMETRY_PARAMETERS, RADAR_TYPE_I, resolve_radar_altitude_layer_id,
)
from cns_planner.domain.radar_terrain_los import (  # noqa: E402
    radar_origin_above_terrain_scenario,
)
from cns_planner.gis import radar_terrain_los_adapter as terrain_los  # noqa: E402
from cns_planner.gis.radar_layout_adapter import resolve_radar_origins  # noqa: E402

#: 真实 FABDEM 舟山切片的默认路径（只读；不存在时脚本显式失败，绝不换数据源）。
DEFAULT_TERRAIN = (
    r"D:\aaa2026project\UOM\舟山\规划系统\FABDEM\processed\Zhoushan_FABDEM_DTM_30m.tif"
)

#: probe 采样配置：距离（米）与方位（度）。方位只用于构造 probe 位置，
#: **不**参与任何波束几何判定（本模块不替换既有冻结几何模型）。
PROBE_DISTANCES_M = (250.0, 800.0, 1500.0, 2500.0)
PROBE_AZIMUTHS_DEG = (0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0)

#: 抽样塔的选取规则（覆盖任务指定的样本类型）。
SAMPLE_SELECTORS = (
    ("resolved_ground_tower_top", lambda row: row["origin_tier"] == "tower_obstacle_profile"
     and row["base_type"] == "ground"),
    ("resolved_rooftop_tower_top", lambda row: row["origin_tier"] == "tower_obstacle_profile"
     and row["base_type"] == "rooftop"),
    ("source_planning_estimate", lambda row: row["origin_tier"] == "source_planning_estimate"),
    ("largest_tower_top_minus_source_delta",
     lambda row: row["origin_tier"] == "tower_obstacle_profile"),
    ("highest_fabdem_terrain", lambda row: True),
    ("origin_not_above_terrain", lambda row: row["origin_not_above_terrain"] is True),
    ("coastal_low_terrain", lambda row: True),
)


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _metric_transform(metric_crs):
    from pyproj import Transformer

    forward = Transformer.from_crs("EPSG:4326", metric_crs, always_xy=True)
    backward = Transformer.from_crs(metric_crs, "EPSG:4326", always_xy=True)

    class _Transform:
        method = "pyproj_projected_crs_coordinate_transform"

        def to_metric(self, point):
            x, y = forward.transform(float(point[0]), float(point[1]))
            return [float(x), float(y)]

        def to_geographic(self, point):
            lon, lat = backward.transform(float(point[0]), float(point[1]))
            return [float(lon), float(lat)]

        def describe(self):
            return {"method": self.method, "metric_crs": metric_crs,
                    "geographic_crs": "EPSG:4326"}

    return _Transform()


def _offset_point(transform, longitude, latitude, *, east_m, north_m):
    origin = transform.to_metric([longitude, latitude])
    if origin is None:
        return None
    target = [origin[0] + east_m, origin[1] + north_m]
    return transform.to_geographic(target)


def _probe_specs(row, altitude_layers):
    """构造抽样塔的 probe 列表（高度全部来自项目高度层目录）。"""

    catalog = {item["altitude_layer_id"]: item for item in altitude_layers}
    probes = []
    layer_cycle = [item["altitude_layer_id"] for item in altitude_layers]
    index = 0
    for distance in PROBE_DISTANCES_M:
        for azimuth in PROBE_AZIMUTHS_DEG:
            layer_id = layer_cycle[index % len(layer_cycle)]
            index += 1
            entry = catalog.get(layer_id) or {}
            probes.append({
                "probe_id": f"{row['tower_id']}@{distance:.0f}@{azimuth:.0f}@{layer_id}",
                "distance_m": distance,
                "azimuth_deg": azimuth,
                "altitude_layer_id": layer_id,
                "nominal_altitude_m": entry.get("nominal_altitude_m"),
            })
    return probes


def _select_samples(rows, *, limit_per_selector=3):
    samples = {}
    for name, predicate in SAMPLE_SELECTORS:
        candidates = [row for row in rows if row["origin_egm2008_m"] is not None]
        if name == "largest_tower_top_minus_source_delta":
            candidates = [
                row for row in candidates
                if row["tower_top_orthometric_m"] is not None
                and row["source_estimate_m"] is not None
            ]
            candidates.sort(
                key=lambda row: -abs(row["tower_top_orthometric_m"] - row["source_estimate_m"])
            )
        elif name == "highest_fabdem_terrain":
            candidates = [row for row in candidates if row["terrain_egm2008_m"] is not None]
            candidates.sort(key=lambda row: -row["terrain_egm2008_m"])
        elif name == "coastal_low_terrain":
            candidates = [
                row for row in candidates
                if row["terrain_egm2008_m"] is not None and abs(row["terrain_egm2008_m"]) < 20.0
            ]
            candidates.sort(key=lambda row: abs(row["terrain_egm2008_m"]))
        elif name == "origin_not_above_terrain":
            candidates = [
                row for row in candidates if row["origin_minus_terrain_m"] is not None
            ]
            candidates.sort(key=lambda row: row["origin_minus_terrain_m"])
        else:
            candidates.sort(key=lambda row: str(row["tower_id"]))
        picked = [row for row in candidates if predicate(row)][:limit_per_selector]
        samples[name] = picked
    return samples


def main(argv=None):
    parser = argparse.ArgumentParser(description="Round34-T07-A 真实数据地形 LOS 只读验证")
    parser.add_argument("--towers", required=True)
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--terrain", default=DEFAULT_TERRAIN)
    parser.add_argument("--out", required=True)
    parser.add_argument("--metric-crs", default="EPSG:32651")
    parser.add_argument("--state", default=None, help="可选：真实 ProjectState（只读，用于高度层解析）")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    towers = _load_json(args.towers)
    profiles = _load_json(args.profiles)
    policy = _load_json(args.policy)
    project_state = _load_json(args.state) if args.state else None
    default_catalog = {
        str(item["altitude_layer_id"]): item for item in default_altitude_layers()
        if isinstance(item, dict) and item.get("altitude_layer_id")
    }
    catalog = default_catalog
    if isinstance(project_state, dict):
        state_layers = {
            item["altitude_layer_id"]: item
            for item in ((project_state.get("spatial_3d") or {}).get("altitude_layers") or [])
            if isinstance(item, dict) and item.get("altitude_layer_id")
        }
        catalog = state_layers or default_catalog
    altitude_layers = list(catalog.values())
    layer_state = (
        project_state if isinstance(project_state, dict)
        else {"radar_surveillance_policy": policy}
    )
    resolved_layer = resolve_radar_altitude_layer_id(layer_state, layers=catalog)
    print(f"altitude layer resolved  : {resolved_layer[0]} / {resolved_layer[1]} m "
          f"({resolved_layer[2]}, ambiguous={resolved_layer[4]})")

    transform = _metric_transform(args.metric_crs)
    cache = terrain_los.TerrainSourceCache()
    #: 两个访问器变体都来自既有已验证实现，且各自**只打开一次**：
    #: ``window`` 用于既有的逐点/逐格地形采样，``native`` 用于沿射线 native 像素剖面。
    point_source = cache.get(args.terrain, terrain_los.SOURCE_VARIANT_WINDOW)
    source = cache.get(args.terrain, terrain_los.SOURCE_VARIANT_NATIVE)
    if source is None or point_source is None:
        print(f"FATAL: terrain source unavailable: {cache.describe()['unavailable']}")
        return 2
    usable = source.usable()
    print(f"terrain usable           : {usable} vertical={source.vertical_reference}")
    source_identity = terrain_los.terrain_source_identity(source)
    print(f"terrain identity         : {json.dumps(source_identity, ensure_ascii=False)}")

    # ---- 1. 原点解析（复用既有链路） ----------------------------------------
    origin_result = resolve_radar_origins(
        towers=list(towers), obstacle_profiles=profiles, mount_assumption={},
        planning_policy_id=policy.get("radar_planning_origin_policy_id"),
        device_origins=list(towers),
    )
    records = origin_result["records"]
    profile_items = profiles.get("items") if isinstance(profiles.get("items"), dict) else {}
    print(f"origin state counts      : {origin_result['origin_state_counts']}")
    print(f"origin tier counts       : {origin_result['origin_tier_counts']}")

    # ---- 2. 地形采样与物理输入诊断 ------------------------------------------
    physical = terrain_los.radar_origin_terrain_physical_inputs(
        records, terrain_source=point_source,
    )
    rows = []
    for record in records:
        tower_id = str(record["tower_id"])
        profile = profile_items.get(tower_id) or {}
        physical_record = physical.get(tower_id) or {}
        tower_top = record.get("tower_top_orthometric_m")
        source_estimate = (
            None if record.get("source_elevation_m") is None
            or record.get("tower_height_m") is None
            else record["source_elevation_m"] + record["tower_height_m"]
        )
        scenario = None
        if physical_record.get("scenario_offered"):
            scenario = radar_origin_above_terrain_scenario(
                origin_physical_input_result=physical_record,
                terrain_m=physical_record.get("terrain_egm2008_m"),
                assumed_structure_height_m=record.get("tower_height_m"),
                assumed_building_height_m=(
                    record.get("building_height_m") if profile.get("base_type") == "rooftop"
                    else None
                ),
            )
        rows.append({
            "tower_id": tower_id,
            "name": record.get("name"),
            "site_type": record.get("site_type"),
            "base_type": profile.get("base_type"),
            "profile_vertical_status": record.get("tower_obstacle_vertical_status"),
            "longitude": record.get("longitude"),
            "latitude": record.get("latitude"),
            "source_elevation_m": record.get("source_elevation_m"),
            "tower_height_m": record.get("tower_height_m"),
            "source_estimate_m": source_estimate,
            "tower_top_orthometric_m": tower_top,
            "building_height_m": record.get("building_height_m"),
            "origin_state": record.get("origin_state"),
            "origin_tier": record.get("origin_tier"),
            "origin_egm2008_m": record.get("origin_egm2008_m"),
            "origin_source": record.get("origin_source"),
            "origin_evidence_level": record.get("origin_evidence_level"),
            "origin_availability": physical_record.get("origin_availability"),
            "terrain_consistency": physical_record.get("terrain_consistency"),
            "terrain_egm2008_m": physical_record.get("terrain_egm2008_m"),
            "origin_minus_terrain_m": physical_record.get("origin_terrain_delta_m"),
            "origin_not_above_terrain": (
                None if physical_record.get("origin_terrain_delta_m") is None
                else bool(physical_record["origin_terrain_delta_m"] <= 0.0)
            ),
            "terrain_sampling_status": (physical_record.get("terrain_sampling") or {}).get("status"),
            "physical_reason": physical_record.get("reason"),
            "scenario_status": (scenario or {}).get("status"),
            "scenario_origin_egm2008_m": (scenario or {}).get("scenario_origin_egm2008_m"),
            "scenario_missing_inputs": ";".join((scenario or {}).get("missing_scenario_inputs") or []),
        })
    (out_dir / "tower_terrain_physical_inputs.json").write_text(
        json.dumps({
            "terrain_source": source_identity,
            "metric_crs": args.metric_crs,
            "resolved_altitude_layer": {
                "altitude_layer_id": resolved_layer[0], "nominal_altitude_m": resolved_layer[1],
                "source": resolved_layer[2], "ambiguous": resolved_layer[4],
            },
            "origin_state_counts": deepcopy(origin_result["origin_state_counts"]),
            "origin_tier_counts": deepcopy(origin_result["origin_tier_counts"]),
            "records": physical,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_csv(out_dir / "tower_terrain_physical_inputs.csv", rows)

    availability_counts = Counter(row["origin_availability"] for row in rows)
    consistency_counts = Counter(row["terrain_consistency"] for row in rows)
    print(f"origin availability      : {dict(availability_counts)}")
    print(f"terrain consistency      : {dict(consistency_counts)}")

    # ---- 3. 抽样塔 × probe 的地形 LOS ---------------------------------------
    samples = _select_samples(rows, limit_per_selector=3)
    chosen = {}
    for name, picked in samples.items():
        for row in picked:
            chosen[row["tower_id"]] = row
    print(f"sampled towers           : {len(chosen)} -> {sorted(chosen)}")

    parameters = RADAR_GEOMETRY_PARAMETERS[RADAR_TYPE_I]
    evaluations = []
    started = time.perf_counter()
    for row in chosen.values():
        probes = _probe_specs(row, altitude_layers)
        for probe_index, probe in enumerate(probes):
            radians = math.radians(probe["azimuth_deg"])
            offset = _offset_point(
                transform, row["longitude"], row["latitude"],
                east_m=probe["distance_m"] * math.cos(radians),
                north_m=probe["distance_m"] * math.sin(radians),
            )
            if offset is None:
                continue
            probe_payload = {
                "voxel_id": probe["probe_id"],
                "longitude": offset[0],
                "latitude": offset[1],
                "altitude_layer_id": probe["altitude_layer_id"],
            }
            evaluation = terrain_los.evaluate_probe_terrain_los(
                row, probe_payload, terrain_source=source, transform=transform,
                altitude_layer_id=probe["altitude_layer_id"],
                nominal_altitude_m=probe["nominal_altitude_m"],
                sampling_interval_m=terrain_los.DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
                envelope_radius_m=terrain_los.DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
                earth_radius_m=terrain_los.EARTH_MEAN_RADIUS_M,
                k_factor=terrain_los.DEFAULT_REFRACTION_K_FACTOR,
                origin_physical_input=physical.get(row["tower_id"]),
                origin_availability=row["origin_availability"],
                max_slant_range_m=parameters["max_slant_range_m"],
                #: 每座抽样塔只保留一条**完整**剖面（首个 probe），其余走摘要：
                #: 3 km 射线 × 5 m 采样是 600+ 格，全量序列化会让产物膨胀到上百 MB。
                profile_detail=(
                    terrain_los.PROFILE_DETAIL_FULL if probe_index == 0
                    else terrain_los.PROFILE_DETAIL_SUMMARY
                ),
            )
            evaluation["probe_distance_m"] = probe["distance_m"]
            evaluation["probe_azimuth_deg"] = probe["azimuth_deg"]
            evaluation["tower_base_type"] = row["base_type"]
            evaluations.append(evaluation)
    elapsed = time.perf_counter() - started
    print(f"evaluations              : {len(evaluations)} in {elapsed:.1f} s "
          f"({elapsed / max(1, len(evaluations)) * 1000:.0f} ms each)")
    print(f"los status counts        : {dict(Counter(item['status'] for item in evaluations))}")

    # ---- 4. 确定性：同一输入重复评估 ----------------------------------------
    determinism = _determinism_check(
        chosen, physical, terrain_los, transform, source, altitude_layers,
        parameters, rows,
    )

    payload = {
        "terrain_source": source_identity,
        "metric_crs": args.metric_crs,
        "radar_geometry_parameters": deepcopy(parameters),
        "frozen_geometry_reference": {
            "elevation_center_deg": ELEVATION_CENTER_DEG,
            "elevation_min_deg": ELEVATION_MIN_DEG,
            "elevation_max_deg": ELEVATION_MAX_DEG,
            "replaced_by_terrain_los": False,
            "terrain_los_is_detection_probability": False,
        },
        "sampling_defaults": {
            "sampling_interval_m": terrain_los.DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
            "envelope_radius_m": terrain_los.DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
            "earth_radius_m": terrain_los.EARTH_MEAN_RADIUS_M,
            "k_factor": terrain_los.DEFAULT_REFRACTION_K_FACTOR,
        },
        "origin_availability_counts": dict(availability_counts),
        "terrain_consistency_counts": dict(consistency_counts),
        "los_status_counts": dict(Counter(item["status"] for item in evaluations)),
        "terrain_source_cache": cache.describe(),
        "evaluation_count": len(evaluations),
        "evaluation_seconds": round(elapsed, 3),
        "determinism": determinism,
        "evaluations": evaluations,
    }
    (out_dir / "terrain_los_sample_evaluations.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (out_dir / "terrain_los_summary.json").write_text(
        json.dumps({
            "terrain_source": source_identity,
            "origin_availability_counts": dict(availability_counts),
            "terrain_consistency_counts": dict(consistency_counts),
            "los_status_counts": dict(Counter(item["status"] for item in evaluations)),
            "los_status_by_availability": {
                f"{availability}|{status}": count for (availability, status), count in Counter(
                    (item["radar_origin_availability"], item["status"]) for item in evaluations
                ).items()
            },
            "clearance_stats_clear_only": _stats([
                item["minimum_clearance_m"] for item in evaluations
                if item["status"] == "clear" and item["minimum_clearance_m"] is not None
            ]),
            "evaluation_seconds": round(elapsed, 3),
            "terrain_source_cache": cache.describe(),
            "determinism": determinism,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cache.close()
    return 0


def _determinism_check(chosen, physical, terrain_los, transform, source, altitude_layers,
                       parameters, rows):
    del rows
    if not chosen:
        return {"checked": 0}
    row = next(iter(chosen.values()))
    probes = _probe_specs(row, altitude_layers)[:6]
    fingerprints, statuses = set(), set()
    checked = 0
    for probe in probes:
        offset = _offset_point(
            transform, row["longitude"], row["latitude"],
            east_m=probe["distance_m"], north_m=0.0,
        )
        if offset is None:
            continue
        checked += 1
        payload = {
            "voxel_id": probe["probe_id"], "longitude": offset[0], "latitude": offset[1],
            "altitude_layer_id": probe["altitude_layer_id"],
        }
        for _repeat in range(3):
            evaluation = terrain_los.evaluate_probe_terrain_los(
                row, payload, terrain_source=source, transform=transform,
                altitude_layer_id=probe["altitude_layer_id"],
                nominal_altitude_m=probe["nominal_altitude_m"],
                earth_radius_m=terrain_los.EARTH_MEAN_RADIUS_M,
                k_factor=terrain_los.DEFAULT_REFRACTION_K_FACTOR,
                origin_physical_input=physical.get(row["tower_id"]),
                origin_availability=row["origin_availability"],
                max_slant_range_m=parameters["max_slant_range_m"],
            )
            fingerprints.add(evaluation["input_fingerprint"])
            statuses.add(evaluation["status"])
    return {
        "checked_probe_count": checked,
        "repeats_per_probe": 3,
        "distinct_fingerprints": len(fingerprints),
        "distinct_statuses": sorted(statuses),
        "deterministic": len(fingerprints) == checked,
    }


def _stats(values):
    items = [float(item) for item in values if item is not None]
    if not items:
        return {"count": 0}
    items.sort()
    return {
        "count": len(items), "min": round(items[0], 3), "max": round(items[-1], 3),
        "mean": round(sum(items) / len(items), 3),
        "median": round(items[len(items) // 2], 3),
    }


CSV_COLUMNS = (
    "tower_id", "name", "site_type", "base_type", "profile_vertical_status",
    "longitude", "latitude", "source_elevation_m", "tower_height_m", "source_estimate_m",
    "tower_top_orthometric_m", "building_height_m", "origin_state", "origin_tier",
    "origin_egm2008_m", "origin_source", "origin_evidence_level", "origin_availability",
    "terrain_consistency", "terrain_egm2008_m", "origin_minus_terrain_m",
    "origin_not_above_terrain", "terrain_sampling_status", "physical_reason",
    "scenario_status", "scenario_origin_egm2008_m", "scenario_missing_inputs",
)


def _write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in CSV_COLUMNS})


if __name__ == "__main__":
    raise SystemExit(main())
