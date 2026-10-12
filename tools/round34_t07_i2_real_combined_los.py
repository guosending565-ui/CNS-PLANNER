"""Round34-T07-I2：真实 FABDEM + buildings v3 联合 LOS 只读诊断（端到端走 Application）。

本工具**不**直接调用底层几何函数：它构造一个最小、隔离、只读的项目状态，然后调用
:class:`~cns_planner.application.radar_terrain_los_diagnostics_service.RadarTerrainLosDiagnosticsService`
的 ``combined_status`` / ``diagnose_combined`` / ``combined_details``，因此验证的是**接线**
而不是孤立算法。

只读保证：

* 只读打开 FABDEM 与 buildings v3，运行前后逐字节比较两者的 SHA-256；
* 不写任何 ProjectState、不写 artifact、不注册失效入口（服务本身没有 writer）；
* 端点由**真实建筑**的地面高程与高度构造，并显式声明为隔离诊断示例（synthetic），
  不代表真实塔站安装位置或现场探测性能。

用法::

    python tools/round34_t07_i2_real_combined_los.py --output <path.json>
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cns_planner.application.radar_terrain_los_diagnostics_service import (  # noqa: E402
    RadarTerrainLosDiagnosticsService,
)
from cns_planner.domain.radar_surveillance_layout import (  # noqa: E402
    METRIC_CRS, RADAR_TYPE_I,
)

DEFAULT_BUILDINGS = Path(
    r"D:\aaa2026project\UOM\舟山\规划系统\building\processed\zhoushan_buildings_v3.gpkg"
)
DEFAULT_TERRAIN = Path(
    r"D:\aaa2026project\UOM\舟山\规划系统\FABDEM\processed\Zhoushan_FABDEM_DTM_30m.tif"
)
DEFAULT_OUTPUT = Path(
    r"D:\Projects\_cns_workspaces\deliverables\round34_t07_i2\evidence"
    r"\round34_t07_i2_real_combined_los.json"
)
ROUTE_ID = "T07-I2-ISOLATED-DIAGNOSTIC-ROUTE"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pick_feature(path: Path, levels: tuple[str, ...]) -> int | None:
    """只读挑选一个具备可用高度证据的真实建筑（fail-closed：找不到就返回 None）。"""

    placeholders = ",".join("?" for _ in levels)
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


class ReadOnlySession:
    """最小只读会话：只暴露 ``state``，并记录任何伪装的写入企图。"""

    def __init__(self, state):
        self.state = state
        self.save_count = 0

    def save(self, *args, **kwargs):  # pragma: no cover - 只读诊断绝不调用
        self.save_count += 1
        raise AssertionError("只读联合诊断绝不能保存 ProjectState")


class FixtureRadarLayoutService:
    """只读依赖替身：只提供路径与显式米制变换，不参与任何计算。"""

    def __init__(self, paths, transform):
        self._paths = dict(paths)
        self.metric_transform = transform

    def _source_paths(self, *, facts_provider=None):
        return dict(self._paths)


def build_case(*, source, transform, fid: int, terrain_path: Path, buildings_path: Path,
               evidence_label: str, metric_crs: str, terrain_source=None):
    """用**真实建筑**构造一条隔离射线，并返回可直接喂给服务的只读项目状态。"""

    from qgis.core import (
        QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject,
    )

    feature = source.layer.getFeature(fid)
    if not feature.isValid():
        raise RuntimeError(f"building feature {fid} unavailable")
    context = QgsProject.instance().transformContext()
    to_metric = QgsCoordinateTransform(source.layer.crs(), source.metric_crs, context)
    geometry = QgsGeometry(feature.geometry())
    geometry.transform(to_metric)
    point = geometry.pointOnSurface().asPoint()
    centre_x, centre_y = float(point.x()), float(point.y())

    # 起点放在建筑一侧 600 m 外，终点穿过建筑另一侧，保证同一条射线真的与建筑相交。
    origin_x = centre_x + 600.0
    probe_start_x = centre_x + 500.0
    probe_end_x = centre_x - 200.0

    discovery = source.query_ray_bbox(
        [probe_start_x, centre_y, probe_end_x, centre_y], transform=transform,
        terrain_source=terrain_source, max_features=64,
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
    # 射线高度取建筑半高：低于屋顶，因此建筑域应当给出确定结论（或严格 unknown）。
    ray_z = float(ground) + float(height) * 0.5

    tower_metric = [origin_x, centre_y]
    tower_geographic = transform.to_geographic(tower_metric)
    tower_id = f"T07I2-{fid}"
    state = {
        "data_source_paths": {
            "terrain_dtm": str(terrain_path), "buildings": str(buildings_path),
        },
        "radar_surveillance_policy": {"optimization_sample_spacing_m": 25.0},
        "towers": {"items": [{
            "tower_id": tower_id,
            "longitude": float(tower_geographic[0]), "latitude": float(tower_geographic[1]),
            "name": f"T07-I2 isolated diagnostic endpoint (building {fid})",
        }]},
        "tower_obstacle_profiles": {"items": {tower_id: {
            "tower_top_orthometric_m": ray_z, "status": "passed",
        }}},
        "spatial_3d": {"altitude_layers": [{
            "altitude_layer_id": "T07-I2-DIAGNOSTIC",
            "nominal_altitude_m": ray_z,
            "vertical_reference": "egm2008_orthometric",
        }]},
        "operational_routes": [{
            "route_id": ROUTE_ID, "status": "passed",
            "metric_path": [[probe_start_x, centre_y], [probe_end_x, centre_y]],
        }],
    }
    session = ReadOnlySession(state)
    service = RadarTerrainLosDiagnosticsService(
        session, FixtureRadarLayoutService(state["data_source_paths"], transform), None,
    )
    return {
        "case": evidence_label,
        "building_id": str(fid),
        "height_evidence_level": record.get("height_evidence_level"),
        "origin_metric": tower_metric,
        "origin_geographic": [float(item) for item in tower_geographic],
        "ray_height_egm2008_m": ray_z,
        "building_ground_egm2008_m": float(ground),
        "building_height_estimate_m": float(height),
        "synthetic_endpoint_disclosure": (
            "端点由真实建筑地面高程与高度构造，仅用于只读同射线双源诊断，"
            "不代表真实塔站安装位置或现场探测性能。"
        ),
        "session": session,
        "service": service,
        "metric_crs": metric_crs,
    }


def run_case(case, *, radar_type=RADAR_TYPE_I, probe_budget: int = 6) -> dict:
    service = case.pop("service")
    session = case.pop("session")
    started = time.perf_counter()
    status = service.combined_status()
    evidence = service.diagnose_combined({
        "tower_ids": [f"T07I2-{case['building_id']}"],
        "probe_budget": probe_budget,
        "max_tower_count": 1,
        "altitude_layer_id": "T07-I2-DIAGNOSTIC",
    })
    elapsed = time.perf_counter() - started
    details = service.combined_details({"limit": 20})
    return {
        **case,
        "radar_type": radar_type,
        "status_before_diagnosis": {
            "evidence_status": status.get("evidence_status"),
            "available_to_compute": status.get("available_to_compute"),
            "open_panel_never_computes_los": status.get("open_panel_never_computes_los"),
            "terrain_opened": (status.get("adapter_availability") or {})
            .get("terrain_dtm", {}).get("opened"),
            "buildings_opened": (status.get("adapter_availability") or {})
            .get("buildings", {}).get("opened"),
        },
        "evidence": evidence,
        "detail_page": details.get("detail_page"),
        "detail_recomputed": details.get("recomputed"),
        "elapsed_seconds": elapsed,
        "project_state_save_count": session.save_count,
    }


def summarise(result: dict) -> dict:
    evidence = result["evidence"]
    aggregate = evidence.get("aggregate") or {}
    rays = evidence.get("rays") or []
    return {
        "case": result["case"],
        "building_id": result["building_id"],
        "height_evidence_level": result["height_evidence_level"],
        "evidence_status": evidence.get("evidence_status"),
        "failure_reason": evidence.get("failure_reason"),
        "evaluated_ray_count": evidence.get("evaluated_ray_count"),
        "combined_counts": aggregate.get("combined_los_status_counts"),
        "terrain_counts": aggregate.get("terrain_los_status_counts"),
        "building_counts": aggregate.get("building_los_status_counts"),
        "trustworthy_rays": aggregate.get("trustworthy_obstruction_ray_count"),
        "predictive_rays": aggregate.get("predictive_obstruction_ray_count"),
        "geometry_filtered_rays": aggregate.get("geometry_filtered_ray_count"),
        "first_ray": ({
            "terrain": rays[0].get("terrain_los_status"),
            "building": rays[0].get("building_los_status"),
            "combined": rays[0].get("combined_los_status"),
            "height_evidence_counts": rays[0].get("height_evidence_counts"),
            "building_candidate_count": rays[0].get("building_candidate_count"),
        } if rays else None),
        "dem_open_count": evidence.get("dem_open_count"),
        "building_open_count": evidence.get("building_open_count"),
        "input_fingerprint": evidence.get("input_fingerprint"),
        "project_state_save_count": result["project_state_save_count"],
        "elapsed_seconds": round(result["elapsed_seconds"], 6),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--buildings", type=Path, default=DEFAULT_BUILDINGS)
    parser.add_argument("--terrain", type=Path, default=DEFAULT_TERRAIN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--metric-crs", default=METRIC_CRS)
    parser.add_argument("--probe-budget", type=int, default=6)
    args = parser.parse_args(argv)
    for path in (args.buildings, args.terrain):
        if not path.exists():
            raise SystemExit(f"required read-only input missing: {path}")

    before = {"buildings_sha256": sha256(args.buildings), "terrain_sha256": sha256(args.terrain)}

    from osgeo import gdal, osr
    from qgis.core import QgsApplication

    app = QgsApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QgsApplication([], False)
        app.initQgis()
    source = transform = None
    payload = {}
    try:
        gdal.UseExceptions()
        from cns_planner.gis.fine_environment_adapter import (
            NativeTerrainWindowSource, QgisMetricTransform,
        )
        from cns_planner.gis.radar_building_los_adapter import QgisBuildingLosSource

        transform = QgisMetricTransform(args.metric_crs)
        terrain = NativeTerrainWindowSource(args.terrain, gdal=gdal, osr=osr)
        source = QgisBuildingLosSource(args.buildings, metric_crs=args.metric_crs)
        if not source.describe()["spatial_index_available"]:
            raise RuntimeError("buildings v3 provider did not expose its RTree")

        cases = []
        for label, levels in (
            ("verified_or_derived_building_same_ray_dual_source", ("source_tagged", "derived", "verified")),
            ("predicted_building_strict_unknown_guard", ("predicted",)),
        ):
            fid = pick_feature(args.buildings, levels)
            if fid is None:
                cases.append({"case": label, "skipped": "no_building_with_that_height_evidence"})
                continue
            case = build_case(
                source=source, transform=transform, fid=fid, terrain_path=args.terrain,
                buildings_path=args.buildings, evidence_label=label,
                metric_crs=args.metric_crs, terrain_source=terrain,
            )
            cases.append(run_case(case, probe_budget=args.probe_budget))

        payload = {
            "diagnostic_kind": "isolated_read_only_real_dual_source_combined_los",
            "adapter_id": "radar_combined_los_read_only_evidence_v1",
            "authoritative_project_result": False,
            "production_enforced_enabled": False,
            "strict_and_engineering_estimate_separate": True,
            "project_state_written": False,
            "artifacts_written": False,
            "invalidation_writer_registered": False,
            "source_paths": {"buildings": str(args.buildings), "terrain": str(args.terrain)},
            "metric_crs": args.metric_crs,
            "cases": cases,
            "before": before,
        }
    finally:
        source = transform = None
        gc.collect()
        #: QGIS 的 ``exitQgis()`` 在本机 3.44.14 的 OGR/GDAL 驱动清理路径上会触发
        #: 访问违例（``0xC0000005``）。本进程随后用 ``os._exit`` 立即结束，不再需要
        #: 显式清理，因此这里**不**调用 ``exitQgis()``：真正要保证的是只读输入未被改写，
        #: 而它已经在写文件前逐字节核对过。
        _ = owns_app

    after = {"buildings_sha256": sha256(args.buildings), "terrain_sha256": sha256(args.terrain)}
    payload["after"] = after
    payload["all_read_only_inputs_unchanged"] = before == after
    payload["summary"] = [
        summarise(item) if "evidence" in item else item for item in payload["cases"]
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    #: QGIS 进程退出时可能触发访问违例；先把摘要刷出去，再落盘，最后才退出。
    summary = {
        "output": str(args.output),
        "all_read_only_inputs_unchanged": payload["all_read_only_inputs_unchanged"],
        "summary": payload["summary"],
    }
    print(json.dumps(summary, ensure_ascii=False, default=str))
    sys.stdout.flush()
    sys.stderr.flush()
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
