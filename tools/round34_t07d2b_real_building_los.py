r"""Bounded read-only Round34-T07-D2-B probe against buildings v3 + FABDEM.

Run with QGIS Python::

    & 'C:\Program Files\QGIS 3.44.14\bin\python-qgis-ltr.bat' \
        tools/round34_t07d2b_real_building_los.py

The script never opens a project repository and never writes a source dataset.  Its
only write is the explicitly selected diagnostic JSON under this isolated snapshot.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cns_planner.domain import radar_visibility_evidence as visibility  # noqa: E402
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
DEFAULT_OUTPUT = ROOT / "_work" / "round34_t07d2b_building_diagnostic.json"


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


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--buildings", type=Path, default=DEFAULT_BUILDINGS)
    parser.add_argument("--terrain", type=Path, default=DEFAULT_TERRAIN)
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--metric-crs", default="EPSG:32651")
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
        y = float(point.y())
        start_x = float(bounds.xMinimum()) - 5.0
        end_x = float(bounds.xMaximum()) + 5.0

        # A first bounded RTree query resolves canonical FABDEM footprint ground.  The
        # execution query below then exercises the same source cache and exact geometry.
        discovery = source.query_ray_bbox(
            [start_x, y, end_x, y], transform=transform,
            terrain_source=terrain, max_features=32,
        )
        selected_id = str(feature["fid"])
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
        tower = {"tower_id": "isolated-real-building-probe", "metric": [start_x, y],
                 "origin_egm2008_m": ray_z}
        probe = {"probe_id": f"building-{selected_id}-cross-section", "metric": [end_x, y],
                 "altitude_egm2008_m": ray_z}
        execution = BuildingLosExecutionContext(
            building_source=source, terrain_source=terrain, transform=transform,
            max_rays=1, max_queries=1, max_features_per_ray=32,
        )
        result = execution.evaluate(tower, probe, max_slant_range_m=5000.0)
        combined = visibility.combine_visibility(None, result)
        ray = visibility.ray_geometry_3d(
            tower_id=tower["tower_id"], radar_device_id=None, probe_id=probe["probe_id"],
            origin_metric=tower["metric"], origin_egm2008_m=ray_z,
            target_metric=probe["metric"], target_egm2008_m=ray_z,
            horizontal_crs=args.metric_crs, terrain_status="not_evaluated",
            building_status=result["status"], combined_status=combined["combined_visibility"],
            input_fingerprint=result["input_fingerprint"],
        )
        hits = visibility.obstruction_hits_3d(terrain_los=None, building_los=result)
        counts = Counter([result["status"]])
        payload = {
            "diagnostic_kind": "isolated_engineering_simulation_read_only",
            "authoritative_project_result": False,
            "production_radar_enforced": False,
            "terrain_los_executed": False,
            "building_los_executed": True,
            "source_audit": audit,
            "selected_building": {
                "building_id": selected_id,
                "height_estimate_m": height,
                "height_evidence_level": record.get("height_evidence_level"),
                "ground_elevation_egm2008_m": ground,
            },
            "status_counts": {key: counts.get(key, 0) for key in ("clear", "blocked", "unknown")},
            "building_result": result,
            "combined_visibility": combined,
            "ray_geometry_3d": ray,
            "obstruction_hits_3d": hits,
            "building_performance": execution.receipt(),
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
            execution = source = terrain = transform = None
            feature = metric_geometry = to_metric = None
            gc.collect()
            app.exitQgis()

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
        "output": str(args.output), "status_counts": payload["status_counts"],
        "source_unchanged": payload["all_read_only_inputs_unchanged"],
        "elapsed_seconds": payload["elapsed_seconds"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
