r"""Round34-T07-D2-C0 read-only real-building probe against buildings v3 + FABDEM.

Run with QGIS Python::

    & 'C:\Program Files\QGIS 3.44.14\bin\python-qgis-ltr.bat' \
        tools/round34_t07d2c0_real_building_los.py

Two deliberately separate cases are reported:

* ``synthetic_section_geometry_case`` -- the D2-B 42 m cross-section sample, kept as a
  **pure building geometry** unit case.  It is explicitly *not* a valid Radar-I or
  Radar-II detection geometry, because 42 m is far below the frozen minimum slant range.
* ``device_range_valid_case`` -- a real building crossing whose slant range satisfies the
  frozen Radar-I minimum range, with the full frozen geometry gate, adapter, evidence
  policy and 3D hit evidence recorded end to end.

The script never opens a project repository and never writes a source dataset.  Its only
write is the explicitly selected diagnostic JSON under this isolated snapshot.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cns_planner.domain import radar_visibility_evidence as visibility  # noqa: E402
from cns_planner.domain.radar_building_los import frozen_geometry_gate  # noqa: E402
from cns_planner.gis.fine_environment_adapter import (  # noqa: E402
    NativeTerrainWindowSource, QgisMetricTransform,
)
from cns_planner.gis.radar_building_los_adapter import (  # noqa: E402
    BuildingLosExecutionContext, QgisBuildingLosSource,
)

DEFAULT_BUILDINGS = Path(
    r"D:\aaa2026project\UOM\舟山\规划系统\building\processed\zhoushan_buildings_v3.gpkg"
)
DEFAULT_TERRAIN = Path(
    r"D:\aaa2026project\UOM\舟山\规划系统\FABDEM\processed\Zhoushan_FABDEM_DTM_30m.tif"
)
DEFAULT_PROJECT = Path(r"D:\Projects\_cns_r32g\zhoushan_r0005_g")
DEFAULT_OUTPUT = ROOT / "_work" / "round34_t07d2c0_real_building_diagnostic.json"
DEFAULT_RADAR_TYPE = "radar_i"
#: The D2-B cross-section sample: 5 m west of the footprint, 5 m east of it.
SYNTHETIC_SECTION_MARGIN_M = 5.0
#: The device-range-valid case: the ray leaves the mast 120 m from the footprint's east
#: edge, so the slant range satisfies the frozen Radar-I minimum range.
DEVICE_RANGE_VALID_OFFSET_M = 120.0
RAY_HEIGHT_RATIO = 0.5


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_manifest(root):
    directory = Path(root) / "artifacts"
    records = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        stat = path.stat()
        records.append({
            "path": path.relative_to(root).as_posix(), "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "sha256": sha256(path),
        })
    encoded = json.dumps(records, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {"file_count": len(records), "manifest_sha256": hashlib.sha256(encoded).hexdigest()}


def source_audit(path):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        levels = [
            {"height_evidence_level": row[0], "count": row[1], "missing_height_count": row[2]}
            for row in connection.execute(
                "SELECT COALESCE(height_evidence_level, '<NULL>'), COUNT(*), "
                "SUM(CASE WHEN height_estimate_m IS NULL THEN 1 ELSE 0 END) "
                "FROM buildings GROUP BY height_evidence_level ORDER BY 1"
            )
        ]
        feature_count = connection.execute("SELECT COUNT(*) FROM buildings").fetchone()[0]
        fid = connection.execute(
            "SELECT fid FROM buildings WHERE height_evidence_level IN ('source_tagged','derived') "
            "AND height_estimate_m > 0 ORDER BY fid LIMIT 1"
        ).fetchone()[0]
        rtree = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='rtree_buildings_geom'"
        ).fetchone()[0]
        return {
            "feature_count": feature_count, "height_evidence_levels": levels,
            "selected_feature_fid": fid, "rtree_buildings_geom_present": bool(rtree),
        }
    finally:
        connection.close()


def evaluate_case(*, source, terrain, transform, tower, probe, radar_type, max_features=64):
    """One bounded building LOS execution plus its frozen geometry gate record."""

    gate = frozen_geometry_gate(
        radar_type=radar_type, origin_metric=tower["metric"], target_metric=probe["metric"],
        origin_egm2008_m=tower["origin_egm2008_m"],
        target_egm2008_m=probe["altitude_egm2008_m"],
    )
    execution = BuildingLosExecutionContext(
        building_source=source, terrain_source=terrain, transform=transform,
        max_rays=1, max_queries=1, max_features_per_ray=max_features,
    )
    result = execution.evaluate(tower, probe, radar_type=radar_type)
    combined = visibility.combine_visibility(None, result)
    ray = visibility.ray_geometry_3d(
        tower_id=tower["tower_id"], radar_device_id=radar_type, probe_id=probe["probe_id"],
        origin_metric=tower["metric"], origin_egm2008_m=tower["origin_egm2008_m"],
        target_metric=probe["metric"], target_egm2008_m=probe["altitude_egm2008_m"],
        horizontal_crs="EPSG:32651",
        terrain_status="not_evaluated", building_status=result["status"],
        combined_status=combined["combined_visibility"],
        input_fingerprint=result.get("input_fingerprint"),
    )
    hits = visibility.obstruction_hits_3d(terrain_los=None, building_los=result)
    return {
        "radar_type": radar_type,
        "geometry_gate": gate,
        "within_frozen_device_range": bool(gate["within_frozen_device_range"]),
        "device_geometry": gate.get("device_geometry"),
        "tower": tower,
        "probe": probe,
        "building_result": result,
        "combined_visibility": combined,
        "ray_geometry_3d": ray,
        "obstruction_hits_3d": hits,
        "building_performance": execution.receipt(),
        "status_counts": {key: Counter([result["status"]]).get(key, 0)
                          for key in ("clear", "blocked", "unknown")},
        "input_fingerprint": result.get("input_fingerprint"),
        "execution_identity": result.get("execution_identity"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--buildings", type=Path, default=DEFAULT_BUILDINGS)
    parser.add_argument("--terrain", type=Path, default=DEFAULT_TERRAIN)
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--metric-crs", default="EPSG:32651")
    parser.add_argument("--radar-type", default=DEFAULT_RADAR_TYPE)
    args = parser.parse_args(argv)
    for path in (args.buildings, args.terrain, args.project / "project_state.json"):
        if not path.exists():
            raise SystemExit(f"required read-only input missing: {path}")

    before = {
        "buildings_sha256": sha256(args.buildings),
        "terrain_sha256": sha256(args.terrain),
        "project_state_sha256": sha256(args.project / "project_state.json"),
        "artifacts": artifact_manifest(args.project),
    }
    audit = source_audit(args.buildings)
    started = time.perf_counter()

    from osgeo import gdal, osr
    from qgis.core import QgsApplication, QgsCoordinateTransform, QgsGeometry, QgsProject

    app = QgsApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QgsApplication([], False)
        app.initQgis()
    qgis_objects = []
    payload = {}
    try:
        gdal.UseExceptions()
        transform = QgisMetricTransform(args.metric_crs)
        terrain = NativeTerrainWindowSource(args.terrain, gdal=gdal, osr=osr)
        source = QgisBuildingLosSource(args.buildings, metric_crs=args.metric_crs)
        qgis_objects.extend([source, terrain, transform])
        if not source.describe()["spatial_index_available"]:
            raise RuntimeError("buildings v3 provider did not expose its RTree")

        feature = source.layer.getFeature(int(audit["selected_feature_fid"]))
        if not feature.isValid():
            raise RuntimeError("selected trusted building feature unavailable")
        context = QgsProject.instance().transformContext()
        to_metric = QgsCoordinateTransform(source.layer.crs(), source.metric_crs, context)
        metric_geometry = QgsGeometry(feature.geometry())
        metric_geometry.transform(to_metric)
        point = metric_geometry.pointOnSurface().asPoint()
        bounds = metric_geometry.boundingBox()
        selected_id = str(feature["fid"])
        y = float(point.y())
        x_min, x_max = float(bounds.xMinimum()), float(bounds.xMaximum())

        # --- case 1: pure building geometry section (not a valid device detection) ----
        section_start = x_min - SYNTHETIC_SECTION_MARGIN_M
        section_end = x_max + SYNTHETIC_SECTION_MARGIN_M
        discovery = source.query_ray_bbox(
            [section_start, y, section_end, y], transform=transform,
            terrain_source=terrain, max_features=32,
        )
        record = next(
            (item for item in discovery["buildings"] if str(item.get("building_id")) == selected_id),
            None,
        )
        if record is None:
            raise RuntimeError("RTree ray query did not return selected building")
        ground = record.get("ground_elevation_egm2008_m")
        height = record.get("height_estimate_m")
        if ground is None or height is None:
            raise RuntimeError("selected building prism could not be constructed")
        ray_z = float(ground) + min(float(height) * 0.5, max(0.05, float(height) - 0.01))
        section_tower = {
            "tower_id": "isolated-real-building-probe", "metric": [section_start, y],
            "origin_egm2008_m": ray_z,
        }
        section_probe = {
            "probe_id": f"building-{selected_id}-cross-section",
            "metric": [section_end, y], "altitude_egm2008_m": ray_z,
        }
        section_case = evaluate_case(
            source=source, terrain=terrain, transform=transform,
            tower=section_tower, probe=section_probe, radar_type=args.radar_type,
        )
        section_case["case_kind"] = "pure_building_geometry_unit_case"
        section_case["valid_device_detection_geometry"] = False
        section_case["disclosure"] = (
            "截面射线只有约 42 m，远低于冻结的 Radar-I 最近量程 120 m 与 Radar-II 最近量程 "
            "200 m；它只证明建筑 2.5D 棱柱与共享射线的几何相交，不代表任何型号的有效探测几何。"
        )
        section_slant = section_case["geometry_gate"]["slant_distance_m"]
        section_case["slant_distance_m"] = section_slant

        # --- case 2: a device-range-valid real building crossing ----------------------
        valid_start = x_max - DEVICE_RANGE_VALID_OFFSET_M
        valid_y = y
        valid_tower = {
            "tower_id": "device-range-valid-tower", "metric": [valid_start, valid_y],
            "origin_egm2008_m": None,
        }
        valid_probe = {
            "probe_id": f"building-{selected_id}-device-range-valid",
            "metric": [x_max + max(4.0, (x_max - x_min) * 0.05), valid_y],
            "altitude_egm2008_m": None,
        }
        valid_ray_z = float(ground) + float(height) * RAY_HEIGHT_RATIO
        valid_tower["origin_egm2008_m"] = valid_ray_z
        valid_probe["altitude_egm2008_m"] = valid_ray_z
        valid_case = evaluate_case(
            source=source, terrain=terrain, transform=transform,
            tower=valid_tower, probe=valid_probe, radar_type=args.radar_type,
        )
        valid_case["case_kind"] = "device_range_valid_real_building_case"
        valid_case["valid_device_detection_geometry"] = bool(
            valid_case["within_frozen_device_range"]
        )
        valid_case["device_range_offset_m"] = DEVICE_RANGE_VALID_OFFSET_M
        valid_case["ray_height_ratio_of_building_height"] = RAY_HEIGHT_RATIO
        valid_case["disclosure"] = (
            "该射线的斜距满足冻结设备量程门禁（最小/最大斜距与俯仰区间），并已在同一适配链上"
            "完成建筑棱柱相交、证据政策与 3D 命中记录。"
        )

        payload = {
            "diagnostic_kind": "isolated_engineering_simulation_read_only",
            "authoritative_project_result": False,
            "production_radar_enforced": False,
            "terrain_los_executed": False,
            "building_los_executed": True,
            "radar_type": args.radar_type,
            "source_audit": audit,
            "selected_building": {
                "building_id": selected_id,
                "height_estimate_m": height,
                "height_evidence_level": record.get("height_evidence_level"),
                "ground_elevation_egm2008_m": ground,
            },
            "frozen_device_geometry": section_case["device_geometry"],
            "synthetic_section_geometry_case": section_case,
            "device_range_valid_case": valid_case,
            "discovery_query": discovery.get("query_receipt"),
            "elapsed_seconds": time.perf_counter() - started,
            "before": before,
        }
    finally:
        if owns_app:
            # Provider and GDAL wrappers must die before QGIS tears down its native
            # registries on Windows; otherwise CPython finalization can access freed
            # provider state after main() has already returned successfully.
            qgis_objects.clear()
            source = terrain = transform = None
            feature = metric_geometry = to_metric = None
            record = section_case = valid_case = None
            gc.collect()
            app.exitQgis()
        elif not payload:
            raise

    after = {
        "buildings_sha256": sha256(args.buildings),
        "terrain_sha256": sha256(args.terrain),
        "project_state_sha256": sha256(args.project / "project_state.json"),
        "artifacts": artifact_manifest(args.project),
    }
    payload["after"] = after
    payload["all_read_only_inputs_unchanged"] = before == after
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "synthetic_section": {
            "status": payload["synthetic_section_geometry_case"]["building_result"]["status"],
            "slant_distance_m": payload["synthetic_section_geometry_case"]["slant_distance_m"],
            "within_frozen_device_range": payload[
                "synthetic_section_geometry_case"
            ]["within_frozen_device_range"],
        },
        "device_range_valid": {
            "status": payload["device_range_valid_case"]["building_result"]["status"],
            "slant_distance_m": payload["device_range_valid_case"]["geometry_gate"]["slant_distance_m"],
            "within_frozen_device_range": payload[
                "device_range_valid_case"
            ]["within_frozen_device_range"],
            "hit_building": (
                payload["device_range_valid_case"]["building_result"]["obstruction_hits"][0][
                    "building_id"
                ] if payload["device_range_valid_case"]["building_result"]["obstruction_hits"]
                else None
            ),
        },
        "source_unchanged": payload["all_read_only_inputs_unchanged"],
        "elapsed_seconds": payload["elapsed_seconds"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    _exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # QGIS native teardown can still fault while CPython finalises module globals on
    # Windows; the diagnostic JSON is already on disk at this point, so the process exits
    # without running that finalisation.
    os._exit(_exit_code)
