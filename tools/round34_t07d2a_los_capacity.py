"""Round34-T07-D2-A real R0005 bounded Terrain LOS capacity experiment.

This command is deliberately read-only with respect to the supplied project.  It streams only
the required top-level ProjectState values, performs geometry-only prescans, then runs bounded
LOS batches through the D1 ``TerrainLosExecutionContext``.  Outputs are non-authoritative files
under the caller-selected experiment directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mmap
import sys
import time
import tracemalloc
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cns_planner.algorithms.radar_layout.geometry import (  # noqa: E402
    geometry_evaluation,
)
from cns_planner.algorithms.radar_layout.v1 import build_route_samples  # noqa: E402
from cns_planner.domain.radar_surveillance_layout import (  # noqa: E402
    RADAR_TYPE_I,
    radar_geometry_parameters,
)
from cns_planner.gis.radar_layout_adapter import (  # noqa: E402
    LandMaskSource,
    resolve_radar_origins,
)
from cns_planner.gis.radar_terrain_los_adapter import (  # noqa: E402
    TerrainLosExecutionContext,
)

DEFAULT_PROJECT = Path(r"D:\Projects\_cns_r32g\zhoushan_r0005_g")
EXPECTED_STATE_SHA256 = "D49FEF678FD6B9F2FDAABE526D090C67FF474BF76B03CEE65401E188BE0BE366"
ROUTE_ID = "R0005"
ALTITUDE_LAYER_ID = "ALT-100"
SCENARIOS = (("S1", 16, 64), ("S2", 32, 128), ("S3", 64, 256))
STATE_KEYS = {
    "revision", "project", "spatial_3d", "operational_routes", "towers",
    "tower_obstacle_profiles", "radar_surveillance_policy",
    "layered_route_candidates", "layered_route_validations",
    "layered_operational_adoptions", "artifact_manifest",
}


def _skip_space(data, index):
    while index < len(data) and data[index] in b" \t\r\n":
        index += 1
    return index


def _string_end(data, start):
    index = start + 1
    escaped = False
    while index < len(data):
        byte = data[index]
        if escaped:
            escaped = False
        elif byte == 92:
            escaped = True
        elif byte == 34:
            return index + 1
        index += 1
    raise ValueError("unterminated JSON string")


def _value_end(data, start):
    if data[start] == 34:
        return _string_end(data, start)
    if data[start] in (123, 91):
        depth = 0
        index = start
        while index < len(data):
            byte = data[index]
            if byte == 34:
                index = _string_end(data, index)
                continue
            if byte in (123, 91):
                depth += 1
            elif byte in (125, 93):
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        raise ValueError("unterminated JSON container")
    index = start
    while index < len(data) and data[index] not in (44, 125):
        index += 1
    return index


def _load_selected_top_level(path, keys):
    """Decode selected top-level JSON values without materialising the full state."""

    selected = {}
    with Path(path).open("rb") as handle, mmap.mmap(
        handle.fileno(), 0, access=mmap.ACCESS_READ,
    ) as data:
        index = _skip_space(data, 0)
        if data[index] != 123:
            raise ValueError("ProjectState root must be a JSON object")
        index += 1
        while True:
            index = _skip_space(data, index)
            if data[index] == 125:
                break
            key_end = _string_end(data, index)
            key = json.loads(data[index:key_end].decode("utf-8"))
            index = _skip_space(data, key_end)
            if data[index] != 58:
                raise ValueError("invalid top-level JSON member")
            value_start = _skip_space(data, index + 1)
            value_end = _value_end(data, value_start)
            if key in keys:
                selected[key] = json.loads(data[value_start:value_end].decode("utf-8"))
            index = _skip_space(data, value_end)
            if data[index] == 44:
                index += 1
                continue
            if data[index] == 125:
                break
            raise ValueError("invalid top-level JSON separator")
    return selected


def _stratified(values, limit):
    values = list(values)
    if limit >= len(values):
        return values
    if limit <= 0:
        return []
    if limit == 1:
        return [values[0]]
    indices = [round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)]
    return [values[index] for index in indices]


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _artifact_inventory(root):
    root = Path(root)
    entries = []
    if root.exists():
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            entries.append({
                "path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            })
    fingerprint = hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest().upper()
    return {"file_count": len(entries), "fingerprint": fingerprint}


def _values(value):
    if isinstance(value, dict):
        return list(value.values())
    return value if isinstance(value, list) else []


class _Transform:
    method = "pyproj_projected_crs_coordinate_transform"

    def __init__(self, source_crs="EPSG:4326", metric_crs="EPSG:32651"):
        from pyproj import Transformer

        self.metric_crs = metric_crs
        self._forward = Transformer.from_crs(source_crs, metric_crs, always_xy=True)
        self._backward = Transformer.from_crs(metric_crs, source_crs, always_xy=True)

    def to_metric(self, point):
        x, y = self._forward.transform(float(point[0]), float(point[1]))
        return [float(x), float(y)]

    def to_geographic(self, point):
        x, y = self._backward.transform(float(point[0]), float(point[1]))
        return [float(x), float(y)]


def _distance_to_path(point, path):
    px, py = point
    best = math.inf
    for first, second in zip(path, path[1:]):
        ax, ay = first
        bx, by = second
        dx, dy = bx - ax, by - ay
        denominator = dx * dx + dy * dy
        ratio = 0.0 if denominator == 0.0 else max(
            0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denominator),
        )
        qx, qy = ax + ratio * dx, ay + ratio * dy
        best = min(best, math.hypot(px - qx, py - qy))
    return best


def _representative_towers(records, limit, metric_path):
    usable = [item for item in records if isinstance(item.get("metric"), list)]
    groups = {
        "resolved": [item for item in usable if item.get("origin_tier") == "tower_obstacle_profile"],
        "planning_estimate": [item for item in usable if item.get("planning_estimate_used") is True],
        "other": [item for item in usable if item.get("origin_tier") not in (
            "tower_obstacle_profile", "source_planning_estimate",
        )],
    }
    selected = []
    for group in groups.values():
        ordered = sorted(
            group,
            key=lambda item: (_distance_to_path(item["metric"], metric_path), item["tower_id"]),
        )
        if ordered:
            selected.extend([ordered[0], ordered[-1]])
    by_id = {item["tower_id"]: item for item in selected}
    remaining = [item for item in sorted(usable, key=lambda item: item["tower_id"])
                 if item["tower_id"] not in by_id]
    for item in _stratified(remaining, max(0, limit - len(by_id))):
        by_id[item["tower_id"]] = item
    return list(by_id.values())[:limit]


def _build_samples(route, layer, transform, land_mask):
    metric_path = [transform.to_metric(point[:2]) for point in route["path"]]

    def surface(_index, _offset, metric):
        lon, lat = transform.to_geographic(metric)
        surface_class, evidence = land_mask.classify(lon, lat)
        required = None if surface_class == "unknown" else (
            1 if surface_class == "sea" else 2
        )
        return {
            "surface_class": surface_class,
            "required_distinct_site_count": required,
            "evidence": evidence,
        }

    common = {
        "metric_path": metric_path,
        "route_id": ROUTE_ID,
        "fixed_altitude_m": float(layer["nominal_altitude_m"]),
        "altitude_layer_id": ALTITUDE_LAYER_ID,
        "surface_resolver": surface,
        "coordinate_resolver": transform.to_geographic,
        "include_los_identity": True,
    }
    optimization = build_route_samples(spacing_m=25.0, sample_id_prefix="O", **common)
    validation = build_route_samples(spacing_m=5.0, sample_id_prefix="V", **common)
    return metric_path, optimization, validation


def _geometry_scan(towers, probes, evidence_limit=3):
    parameters = radar_geometry_parameters(RADAR_TYPE_I)
    counts = Counter()
    evidence = {}
    started = time.perf_counter()
    for probe in probes:
        for tower in towers:
            counts["total_combinations"] += 1
            origin = tower.get("origin_egm2008_m")
            if not isinstance(origin, (int, float)) or not isinstance(tower.get("metric"), list):
                status = "geometry_unknown"
            else:
                geometry = geometry_evaluation(
                    origin_egm2008_m=float(origin),
                    sample_egm2008_m=float(probe["egm2008_m"]),
                    sample_metric=probe["metric"], tower_metric=tower["metric"],
                )
                if not (
                    float(parameters["min_slant_range_m"])
                    <= geometry["slant_distance_m"]
                    <= float(parameters["max_slant_range_m"])
                ):
                    status = "out_of_equipment_range"
                elif not (
                    float(parameters["elevation_min_deg"])
                    <= geometry["elevation_deg"]
                    <= float(parameters["elevation_max_deg"])
                ):
                    status = "geometry_filtered"
                else:
                    status = "los_eligible"
            counts[status] += 1
            bucket = evidence.setdefault(status, [])
            if len(bucket) < evidence_limit:
                bucket.append({
                    "tower_id": tower.get("tower_id"),
                    "probe_id": probe.get("sample_id"),
                    "origin_state": tower.get("origin_state"),
                    "origin_tier": tower.get("origin_tier"),
                    "surface_class": probe.get("surface_class"),
                    "status": status,
                })
    counts["geometry_filtered_combinations"] = (
        counts["out_of_equipment_range"] + counts["geometry_filtered"]
    )
    return {
        **dict(counts),
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "evidence_samples": evidence,
        "dem_open_count": 0,
    }


def _scenario_probes(optimization, validation, limit):
    optimization_limit = min(len(optimization), max(1, limit // 2))
    validation_limit = min(len(validation), max(0, limit - optimization_limit))
    return (
        _stratified(optimization, optimization_limit)
        + _stratified(validation, validation_limit)
    )


def _combinations(towers, probes):
    for probe in probes:
        for tower in towers:
            yield {"tower": tower, "sample": probe, "radar_type": RADAR_TYPE_I}


def _feature(record, tower_by_id, probe_by_id, scenario):
    tower = tower_by_id.get(record.get("tower_id")) or {}
    probe = probe_by_id.get(record.get("sample_id")) or {}
    point = None
    if probe.get("longitude") is not None:
        point = [probe["longitude"], probe["latitude"]]
    elif tower.get("longitude") is not None:
        point = [tower["longitude"], tower["latitude"]]
    return {
        "type": "Feature",
        "geometry": None if point is None else {"type": "Point", "coordinates": point},
        "properties": {
            "scenario": scenario,
            "tower_id": record.get("tower_id"),
            "probe_id": record.get("sample_id"),
            "origin_evidence": tower.get("origin_tier"),
            "los_status": record.get("status"),
            "reason": record.get("reason"),
            "radar_type": record.get("radar_type"),
            "input_fingerprint": record.get("cache_key"),
            "calculation_version": "terrain_los_v1+D2A-batch-v1",
            "complete": record.get("status") != "not_sampled",
            "clear_is_detection_confirmation": False,
        },
    }


def _identity(state, state_sha):
    routes = _values(state.get("operational_routes"))
    route = next((item for item in routes if item.get("route_id") == ROUTE_ID), None)
    spatial = state.get("spatial_3d") or {}
    layers = _values(spatial.get("altitude_layers"))
    layer = next((item for item in layers if item.get("altitude_layer_id") == ALTITUDE_LAYER_ID), None)
    operating = next((item for item in _values(spatial.get("route_operating_layers"))
                      if item.get("route_id") == ROUTE_ID), None)
    validations = _values((state.get("layered_route_validations") or {}).get("items"))
    validation = next((item for item in validations
                       if item.get("validation_id") == "LRV-EF3A9B0D7CB7"), None)
    adoptions = _values((state.get("layered_operational_adoptions") or {}).get("items"))
    adoption = next((item for item in adoptions
                     if item.get("adoption_id") == "LRA-0F36B3E0091F"), None)
    policy = state.get("radar_surveillance_policy") or {}
    checks = {
        "revision_442": state.get("revision") == 442,
        "state_sha256": state_sha == EXPECTED_STATE_SHA256,
        "route_r0005_passed": bool(route and route.get("status") == "passed"),
        "alt_100_confirmed": bool(layer and layer.get("confirmed") is True),
        "operating_layer_current": bool(
            operating and operating.get("altitude_layer_id") == ALTITUDE_LAYER_ID
            and operating.get("current_applicability") == "current"
        ),
        "adoption_current": bool(adoption and adoption.get("current_applicability") == "current"),
        "validation_current": bool(validation and validation.get("current_applicability") == "current"),
        "tower_count_373": (state.get("towers") or {}).get("count") == 373,
        "profile_counts_77_296": (
            (state.get("tower_obstacle_profiles") or {}).get("resolved_count") == 77
            and (state.get("tower_obstacle_profiles") or {}).get("unresolved_count") == 296
        ),
        "origin_policy": policy.get("radar_planning_origin_policy_id")
        == "radar_all_tower_rooftop_estimate_v1",
        "radar_i_only": policy.get("allowed_radar_types") == ["radar_i"],
    }
    return {
        "valid": all(checks.values()), "checks": checks, "route": route, "layer": layer,
        "operating_layer": operating, "validation": validation, "adoption": adoption,
        "candidate_items_empty_but_provenance_preserved": (
            not _values((state.get("layered_route_candidates") or {}).get("items"))
            and (route or {}).get("provenance", {}).get("candidate_id")
            == "LRC2-R0005-ALT-100-layeredcandv1-d5"
        ),
    }


def _write_contract(path):
    path.write_text("""# radar_terrain_los_evidence_v1 候选合同（设计稿）

本文件仅是下一轮生产启用设计，不注册 schema、不迁移 ProjectState、不调用正式 writer。

## 最小摘要

- `schema_version` / `evidence_id` / `route_id` / `altitude_layer_id` / `radar_type`。
- 独立 `los_input_fingerprint`：绑定航路几何与高度、origin evidence 指纹、DEM 身份、LOS core 与采样策略、Radar 几何参数；不绑定 Theta*/P14/P15/P16/Required CNS。
- `origin_evidence_refs`：逐塔引用设备声明、tower-top 或获批 planning estimate；unknown/not_evaluable 保持独立。
- `terrain_source`：path locator、variant、size、mtime、sha256/source version、EGM2008 vertical reference。
- `algorithm`：LOS core version、D2-A batch scheduler version、sampling interval/envelope、Radar-I/II preset identity。
- `completion`：`computation_complete`、`evidence_complete`、`search_incomplete`、预算/取消/异常原因。
- `counts`：geometry filtered/unknown、LOS eligible、executed、clear/blocked/unknown、not_sampled、cache、DEM open、terrain samples。
- `batches`：确定顺序规则、batch size、ray/time budget、起止与每批统计。
- `detail_artifact`：外置 locator、media type、byte size、SHA256、record count；ProjectState 不嵌入矩阵。

## current / stale

任一被指纹绑定的 route geometry/ALT、origin evidence、DEM identity、LOS core/sampling、Radar preset 变化即 stale；预算变化只改变执行记录与完成度，也必须形成新 evidence identity。失效方向仅为 LOS evidence → Radar layout；不得反向失效 Theta*、P14/P15/P16 或 Required CNS。权威写入者应是未来单一 Application LOS evidence service，Radar layout 只读 current evidence 引用。

旧 Radar 1.3/几何结果继续按 `terrain_los_mode=disabled` 读取；缺少 LOS evidence 不改写旧结果，只披露 `not_evaluated`。生产启用需人工批准：enforced 的 authority gate、planning-estimate 可用范围、unknown/not_evaluable 处置、预算与重试策略、DEM/采样版本、Radar-II 是否及何时允许、stale/重算与发布规则。
""", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="R0005 bounded Terrain LOS capacity experiment")
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "_work")
    parser.add_argument("--max-los-rays", type=int, default=512)
    parser.add_argument("--max-elapsed-seconds", type=float, default=30.0)
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    state_path = args.project / "project_state.json"
    artifacts_path = args.project / "artifacts"
    state_sha_before = _sha256(state_path)
    artifacts_before = _artifact_inventory(artifacts_path)
    state = _load_selected_top_level(state_path, STATE_KEYS)
    identity = _identity(state, state_sha_before)
    if not identity["valid"]:
        raise SystemExit("real project identity gate failed; refusing real LOS execution")

    data_sources = json.loads((args.project / "data_sources.json").read_text(encoding="utf-8-sig"))
    terrain_path = data_sources["terrain_dtm"]
    policy = state["radar_surveillance_policy"]
    route, layer = identity["route"], identity["layer"]
    transform = _Transform(source_crs=route.get("path_crs") or "EPSG:4326")
    land_mask = LandMaskSource(
        data_sources.get("land_mask"), layer_name=policy.get("land_mask_layer_name"),
        coastal_uncertainty_buffer_m=policy.get("coastal_uncertainty_buffer_m"),
    )
    metric_path, optimization, validation = _build_samples(route, layer, transform, land_mask)
    optimization_samples = optimization["samples"]
    validation_samples = validation["samples"]

    towers = _values(state["towers"].get("items"))
    origins = resolve_radar_origins(
        towers=towers, obstacle_profiles=state["tower_obstacle_profiles"],
        mount_assumption=policy.get("radar_mount_height"),
        planning_policy_id=policy.get("radar_planning_origin_policy_id"),
        device_origins=towers,
    )
    origin_records = origins["records"]
    for item in origin_records:
        if item.get("longitude") is not None and item.get("latitude") is not None:
            item["metric"] = transform.to_metric([item["longitude"], item["latitude"]])
        item["origin_availability"] = (
            "usable" if item.get("origin_egm2008_m") is not None and item.get("metric")
            else "not_evaluable"
        )
    origin_categories = Counter({
        "origin_resolved": 0, "planning_estimate": 0,
        "unknown": 0, "not_evaluable": 0,
    })
    for item in origin_records:
        if item["origin_availability"] == "not_evaluable":
            origin_categories["not_evaluable"] += 1
        elif item.get("planning_estimate_used"):
            origin_categories["planning_estimate"] += 1
        elif item.get("origin_state") == "unknown":
            origin_categories["unknown"] += 1
        else:
            origin_categories["origin_resolved"] += 1

    geometry = {
        "optimization_25m": _geometry_scan(origin_records, optimization_samples),
        "validation_5m": _geometry_scan(origin_records, validation_samples),
    }
    tower_by_id = {item["tower_id"]: item for item in origin_records}
    probe_by_id = {item["sample_id"]: item for item in optimization_samples + validation_samples}
    scenario_reports = []
    features = []
    for label, tower_limit, probe_limit in SCENARIOS:
        selected_towers = _representative_towers(origin_records, tower_limit, metric_path)
        selected_probes = _scenario_probes(
            optimization_samples, validation_samples, probe_limit,
        )
        tracemalloc.start()
        with TerrainLosExecutionContext(
            terrain_path=terrain_path, mode="enforced", route_id=ROUTE_ID,
            transform=transform, max_ray_count=args.max_los_rays,
        ) as context:
            report = context.lookup.evaluate_batches(
                _combinations(selected_towers, selected_probes),
                batch_size=args.batch_size,
                max_elapsed_seconds=args.max_elapsed_seconds,
                evidence_limit_per_class=3,
            )
            context_snapshot = context.snapshot()
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        report.update({
            "scenario": label,
            "tower_limit": tower_limit,
            "probe_limit": probe_limit,
            "selected_tower_count": len(selected_towers),
            "selected_probe_count": len(selected_probes),
            "selected_25m_probe_count": sum(item["sample_id"].startswith("O") for item in selected_probes),
            "selected_5m_probe_count": sum(item["sample_id"].startswith("V") for item in selected_probes),
            "dem_open_count": context_snapshot["dem_open_count"],
            "python_peak_memory_bytes": peak,
            "python_peak_memory_excludes_gdal_native": True,
            "tower_selection_not_fastest_only": True,
        })
        scenario_reports.append(report)
        for rows in report["evidence_samples"].values():
            features.extend(_feature(item, tower_by_id, probe_by_id, label) for item in rows)

    # Real-input guard evidence for the distinct "not sampled because of budget" state.  This
    # is not a fourth performance scenario and never opens DEM: it exercises the same S3
    # geometry with a deliberate zero-ray budget so downstream maps can display incomplete
    # computation without confusing it with physical unknown/blocked.
    with TerrainLosExecutionContext(
        terrain_path=terrain_path, mode="enforced", route_id=ROUTE_ID,
        transform=transform, max_ray_count=0,
    ) as guard_context:
        budget_guard = guard_context.lookup.evaluate_batches(
            _combinations(selected_towers, selected_probes),
            batch_size=args.batch_size, max_elapsed_seconds=args.max_elapsed_seconds,
            evidence_limit_per_class=3,
        )
        budget_guard["dem_open_count"] = guard_context.snapshot()["dem_open_count"]
    for item in budget_guard["evidence_samples"]["not_sampled"]:
        features.append(_feature(
            item, tower_by_id, probe_by_id, "BUDGET_GUARD_REAL_INPUT",
        ))

    total_geometry = {
        key: sum(scan.get(key, 0) for scan in geometry.values())
        for key in (
            "total_combinations", "geometry_filtered_combinations", "geometry_unknown",
            "los_eligible", "out_of_equipment_range", "geometry_filtered",
        )
    }
    throughput_scenarios = [
        item for item in scenario_reports
        if item.get("computation_complete") and item.get("ray_throughput_per_second")
    ]
    throughputs = [item["ray_throughput_per_second"] for item in throughput_scenarios]
    full_rays = total_geometry["los_eligible"]
    estimate = {
        "estimated_full_los_ray_count": full_rays,
        "observed_throughput_range_rays_per_second": (
            [min(throughputs), max(throughputs)] if throughputs else None
        ),
        "throughput_scenarios": [item["scenario"] for item in throughput_scenarios],
        "estimated_seconds_range": (
            [round(full_rays / max(throughputs), 1), round(full_rays / min(throughputs), 1)]
            if throughputs else None
        ),
        "uncertainty": [
            "sampled rays are deterministic stratified subsets, not a full spatial census",
            "terrain window size and GDAL cache/native memory vary by ray",
            "Python tracemalloc excludes GDAL native allocations",
            "cache reuse in the full optimizer depends on repeated physical probes",
            "elapsed budget is cooperative at batch/ray boundaries; an in-flight GDAL read is not preempted",
        ],
    }

    state_sha_after = _sha256(state_path)
    artifacts_after = _artifact_inventory(artifacts_path)
    capacity = {
        "round": "Round34-T07-D2-A",
        "authoritative": False,
        "project_read_only": True,
        "identity": {key: value for key, value in identity.items() if key not in ("route", "layer")},
        "state_sha256_before": state_sha_before,
        "state_sha256_after": state_sha_after,
        "artifacts_before": artifacts_before,
        "artifacts_after": artifacts_after,
        "formal_project_unchanged": (
            state_sha_before == state_sha_after and artifacts_before == artifacts_after
        ),
        "origin_categories": dict(origin_categories),
        "origin_state_counts": origins.get("origin_state_counts"),
        "origin_tier_counts": origins.get("origin_tier_counts"),
        "route_length_m": optimization["route_length_m"],
        "optimization_25m_probe_count": len(optimization_samples),
        "validation_5m_probe_count": len(validation_samples),
        "surface_counts_25m": dict(Counter(item["surface_class"] for item in optimization_samples)),
        "surface_counts_5m": dict(Counter(item["surface_class"] for item in validation_samples)),
        "geometry_prescreen": geometry,
        "geometry_total": total_geometry,
        "scenarios": scenario_reports,
        "budget_guard_real_input": budget_guard,
        "full_los_resource_estimate": estimate,
        "budgets": {
            "max_los_rays": args.max_los_rays,
            "max_elapsed_seconds": args.max_elapsed_seconds,
            "batch_size": args.batch_size,
        },
        "radar_type": RADAR_TYPE_I,
        "radar_ii_executed": False,
        "milp_executed": False,
    }
    capacity_path = args.out / "round34_t07d2a_capacity.json"
    capacity_path.write_text(json.dumps(capacity, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "round34_t07d2a_samples.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_contract(args.out / "round34_t07d2a_evidence_contract.md")

    rows = []
    for item in scenario_reports:
        counts = item["executed_los_status_counts"]
        rows.append(
            f"| {item['scenario']} | {item['selected_tower_count']} | {item['selected_probe_count']} | "
            f"{item['executed_rays']} | {counts['clear']}/{counts['blocked']}/{counts['unknown']} | "
            f"{item['not_sampled_count']} | {item['dem_open_count']} | "
            f"{item['terrain_sample_count']} | {item['elapsed_seconds']:.3f} |"
        )
    summary = f"""# Round34-T07-D2-A 容量摘要

- 真实项目身份：{'通过' if identity['valid'] else '阻塞'}；revision 442；R0005 / ALT-100 / current adoption-validation。
- 正式 state SHA256：`{state_sha_before}`（前后{'一致' if state_sha_before == state_sha_after else '不一致'}）；artifacts 指纹前后{'一致' if artifacts_before == artifacts_after else '不一致'}。
- 373 塔原点：{dict(origin_categories)}。planning estimate 仅用于本轮获批隔离仿真，不代表现场安装确认。
- 航路长 {optimization['route_length_m']:.1f} m；25 m probes={len(optimization_samples)}；5 m probes={len(validation_samples)}。
- 全量 geometry-only 组合={total_geometry['total_combinations']}；过滤={total_geometry['geometry_filtered_combinations']}；geometry unknown={total_geometry['geometry_unknown']}；LOS eligible={total_geometry['los_eligible']}。

| 场景 | 塔 | Probe | rays | clear/blocked/unknown | not sampled | DEM open | terrain samples | 秒 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

全量 LOS 资源估计：{json.dumps(estimate, ensure_ascii=False)}。该估计仅使用计算完整的场景吞吐，基于有界分层样本，受地形窗口、GDAL 原生缓存和重复射线复用影响，不是完整覆盖评价。elapsed budget 在 batch/射线边界协作检查；正在执行的 native GDAL 射线不会被强杀，overshoot 会显式记录。`clear` 仅表示当前 Terrain LOS 核心判定，不是 Radar 探测确认。

生产启用前仍需批准：enforced authority gate、planning-estimate 适用范围、unknown/not_evaluable 处置、预算/重试、DEM/采样版本、Radar-II 升级规则、stale/发布与旧 Radar 1.3 兼容策略。
"""
    (args.out / "round34_t07d2a_summary.md").write_text(summary, encoding="utf-8")
    print(json.dumps({
        "identity_valid": identity["valid"],
        "origin_categories": dict(origin_categories),
        "route_length_m": optimization["route_length_m"],
        "probe_counts": [len(optimization_samples), len(validation_samples)],
        "geometry_total": total_geometry,
        "scenarios": [{key: item[key] for key in (
            "scenario", "executed_rays", "executed_los_status_counts",
            "los_status_counts", "not_sampled_count",
            "dem_open_count", "terrain_sample_count", "elapsed_seconds",
        )} for item in scenario_reports],
        "formal_project_unchanged": capacity["formal_project_unchanged"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
