"""Bounded read-only real FABDEM + buildings-v3 LOS acceptance for T07-I1."""

from __future__ import annotations

import argparse
import gc
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cns_planner.algorithms.radar_layout.terrain_los_lookup import (  # noqa: E402
    BUILDING_MODE_COMBINED,
    TERRAIN_LOS_MODE_ENFORCED,
    TerrainLosLookup,
)
from cns_planner.domain.radar_surveillance_layout import (  # noqa: E402
    RADAR_TYPE_I,
    radar_geometry_parameters,
)
from cns_planner.gis.fine_environment_adapter import (  # noqa: E402
    NativeTerrainWindowSource,
    QgisMetricTransform,
)
from cns_planner.gis.radar_building_los_adapter import (  # noqa: E402
    QgisBuildingLosSource,
    evaluate_probe_building_los,
)
from cns_planner.gis.radar_terrain_los_adapter import (  # noqa: E402
    DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
    DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
    PROFILE_DETAIL_SUMMARY,
    evaluate_probe_terrain_los,
)
from tools.round34_t07d2c0_real_building_los import (  # noqa: E402
    artifact_manifest,
    sha256,
    source_audit,
)

DEFAULT_BUILDINGS = Path(
    r"D:\aaa2026project\UOM\舟山\规划系统\building\processed\zhoushan_buildings_v3.gpkg"
)
DEFAULT_TERRAIN = Path(
    r"D:\aaa2026project\UOM\舟山\规划系统\FABDEM\processed\Zhoushan_FABDEM_DTM_30m.tif"
)
DEFAULT_PROJECT = Path(r"D:\Projects\_cns_r32g\zhoushan_r0005_g")
DEFAULT_OUTPUT = ROOT / "_work" / "round34_t07_i1_real_combined_los.json"


def _feature_id(path: Path, levels: tuple[str, ...]) -> int | None:
    placeholders = ",".join("?" for _item in levels)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT fid FROM buildings WHERE height_evidence_level IN ("
            f"{placeholders}) AND height_estimate_m > 0 ORDER BY fid LIMIT 1",
            levels,
        ).fetchone()
        return None if row is None else int(row[0])
    finally:
        connection.close()


def _case(*, source, terrain, transform, fid: int, evidence_case: str):
    from qgis.core import QgsCoordinateTransform, QgsGeometry, QgsProject

    feature = source.layer.getFeature(fid)
    if not feature.isValid():
        raise RuntimeError(f"building feature {fid} unavailable")
    context = QgsProject.instance().transformContext()
    to_metric = QgsCoordinateTransform(source.layer.crs(), source.metric_crs, context)
    geometry = QgsGeometry(feature.geometry())
    geometry.transform(to_metric)
    point = geometry.pointOnSurface().asPoint()
    bounds = geometry.boundingBox()
    y = float(point.y())
    x_min, x_max = float(bounds.xMinimum()), float(bounds.xMaximum())
    start_x = x_max - 120.0
    end_x = x_max + max(4.0, (x_max - x_min) * 0.05)

    discovery = source.query_ray_bbox(
        [start_x, y, end_x, y], transform=transform,
        terrain_source=terrain, max_features=64,
    )
    record = next(
        (item for item in discovery["buildings"] if str(item.get("building_id")) == str(fid)),
        None,
    )
    if record is None:
        raise RuntimeError(f"RTree query did not return building {fid}")
    ground = record.get("ground_elevation_egm2008_m")
    height = record.get("height_estimate_m")
    if ground is None or height is None:
        raise RuntimeError(f"building {fid} has no usable prism")
    ray_z = float(ground) + float(height) * 0.5
    tower = {
        "tower_id": f"synthetic-endpoint-{fid}",
        "metric": [start_x, y],
        "origin_egm2008_m": ray_z,
        "origin_source": "actual_building_ground_plus_half_height_diagnostic",
        "origin_state": "confirmed_for_isolated_diagnostic",
        "origin_evidence_level": "verified",
        "origin_availability": "usable",
        "device_instance_id": f"diagnostic-{fid}-{RADAR_TYPE_I}",
    }
    probe = {
        "sample_id": f"building-{fid}-same-ray",
        "metric": [end_x, y],
        "egm2008_m": ray_z,
        "altitude_egm2008_m": ray_z,
        "altitude_layer_id": "T07-I1-DIAGNOSTIC",
    }

    def terrain_evaluator(*, tower, sample, radar_type, geometry):
        del geometry
        parameters = radar_geometry_parameters(radar_type)
        return evaluate_probe_terrain_los(
            tower, sample, terrain_source=terrain, transform=transform,
            altitude_layer_id=sample["altitude_layer_id"],
            nominal_altitude_m=sample["egm2008_m"],
            sampling_interval_m=DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
            envelope_radius_m=DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
            origin_availability=tower["origin_availability"],
            max_slant_range_m=parameters["max_slant_range_m"],
            profile_detail=PROFILE_DETAIL_SUMMARY,
        )

    def building_evaluator(*, tower, sample, radar_type, geometry):
        del geometry
        return evaluate_probe_building_los(
            tower, sample, radar_type=radar_type, building_source=source,
            transform=transform, terrain_source=terrain, max_features=64,
        )

    lookup = TerrainLosLookup(
        mode=TERRAIN_LOS_MODE_ENFORCED,
        evaluator=terrain_evaluator,
        building_evaluator=building_evaluator,
        building_mode=BUILDING_MODE_COMBINED,
        source_identity=terrain.describe(),
        building_source_identity=source.describe(),
        route_id="T07-I1-ISOLATED-DIAGNOSTIC",
        sampling={
            "sampling_interval_m": DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
            "envelope_radius_m": DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
        },
        max_ray_count=1,
    )
    started = time.perf_counter()
    result = lookup.evaluate(tower, probe, RADAR_TYPE_I)
    elapsed = time.perf_counter() - started
    building = result.get("building_los") or {}
    terrain_result = result.get("terrain_los") or {}
    return {
        "case": evidence_case,
        "synthetic_endpoint_disclosure": (
            "端点由真实建筑地面高程与高度构造，仅用于只读同射线双源诊断，"
            "不代表真实塔站安装位置或现场探测性能。"
        ),
        "building_id": str(fid),
        "height_evidence_level": record.get("height_evidence_level"),
        "tower": tower,
        "probe": probe,
        "geometry_gate": {
            key: result.get(key) for key in (
                "horizontal_distance_m", "vertical_delta_m", "slant_distance_m",
                "elevation_deg", "within_equipment_range",
            )
        },
        "terrain": {
            "status": terrain_result.get("status"),
            "reason": terrain_result.get("reason"),
            "sample_count": result.get("terrain_sample_count"),
            "input_fingerprint": terrain_result.get("input_fingerprint"),
            "source": (terrain_result.get("terrain_profile") or {}).get("source"),
            "read_receipt": (terrain_result.get("terrain_profile") or {}).get("read_receipt"),
        },
        "building": {
            "status": building.get("status"),
            "reason": building.get("reason"),
            "candidate_count": building.get("candidate_building_count"),
            "exact_intersection_count": building.get("exact_intersection_count"),
            "height_evidence_counts": building.get("height_evidence_counts"),
            "query_receipt": building.get("query_receipt"),
            "input_fingerprint": building.get("input_fingerprint"),
        },
        "combined_visibility": result.get("combined_visibility_status"),
        "computation_completion": result.get("computation_completion"),
        "evidence_completion": result.get("evidence_completion"),
        "input_fingerprint": result.get("cache_key"),
        "same_ray_both_sources_executed": bool(
            result.get("terrain_sample_count", 0) > 0
            and result.get("building_los_executed")
            and (building.get("query_receipt") or discovery.get("query_receipt"))
        ),
        "elapsed_seconds": elapsed,
        "lookup_statistics": lookup.snapshot(),
        "discovery_query_receipt": discovery.get("query_receipt"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--buildings", type=Path, default=DEFAULT_BUILDINGS)
    parser.add_argument("--terrain", type=Path, default=DEFAULT_TERRAIN)
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--metric-crs", default="EPSG:32651")
    args = parser.parse_args(argv)
    state = args.project / "project_state.json"
    for path in (args.buildings, args.terrain, state):
        if not path.exists():
            raise SystemExit(f"required read-only input missing: {path}")
    before = {
        "buildings_sha256": sha256(args.buildings),
        "terrain_sha256": sha256(args.terrain),
        "project_state_sha256": sha256(state),
        "artifacts": artifact_manifest(args.project),
    }

    from osgeo import gdal, osr
    from qgis.core import QgsApplication

    app = QgsApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QgsApplication([], False)
        app.initQgis()
    source = terrain = transform = None
    payload = {}
    try:
        gdal.UseExceptions()
        transform = QgisMetricTransform(args.metric_crs)
        terrain = NativeTerrainWindowSource(args.terrain, gdal=gdal, osr=osr)
        source = QgisBuildingLosSource(args.buildings, metric_crs=args.metric_crs)
        if not source.describe()["spatial_index_available"]:
            raise RuntimeError("buildings v3 provider did not expose its RTree")
        verified_fid = _feature_id(args.buildings, ("source_tagged", "derived"))
        predicted_fid = _feature_id(args.buildings, ("predicted",))
        if verified_fid is None or predicted_fid is None:
            raise RuntimeError("required verified/predicted building evidence cases unavailable")
        cases = [
            _case(
                source=source, terrain=terrain, transform=transform, fid=verified_fid,
                evidence_case="verified_building_same_ray_real_dual_source",
            ),
            _case(
                source=source, terrain=terrain, transform=transform, fid=predicted_fid,
                evidence_case="predicted_building_strict_unknown_guard",
            ),
        ]
        payload = {
            "diagnostic_kind": "isolated_read_only_real_dual_source_los",
            "authoritative_project_result": False,
            "production_enforced_enabled": False,
            "strict_and_engineering_estimate_separate": True,
            "source_paths": {
                "buildings": str(args.buildings), "terrain": str(args.terrain),
                "project_state": str(state),
            },
            "source_audit": source_audit(args.buildings),
            "cases": cases,
            "same_ray_real_dual_source_proven": any(
                case["same_ray_both_sources_executed"] for case in cases
            ),
            "before": before,
        }
    finally:
        source = terrain = transform = None
        gc.collect()
        if owns_app:
            app.exitQgis()

    after = {
        "buildings_sha256": sha256(args.buildings),
        "terrain_sha256": sha256(args.terrain),
        "project_state_sha256": sha256(state),
        "artifacts": artifact_manifest(args.project),
    }
    payload["after"] = after
    payload["all_read_only_inputs_unchanged"] = before == after
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "same_ray_real_dual_source_proven": payload["same_ray_real_dual_source_proven"],
        "all_read_only_inputs_unchanged": payload["all_read_only_inputs_unchanged"],
        "cases": [{
            "case": case["case"], "terrain": case["terrain"]["status"],
            "terrain_samples": case["terrain"]["sample_count"],
            "building": case["building"]["status"],
            "combined": case["combined_visibility"],
            "completion": [case["computation_completion"], case["evidence_completion"]],
            "elapsed_seconds": case["elapsed_seconds"],
        } for case in payload["cases"]],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
